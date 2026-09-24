"""Encode and decode the stored on-disk/on-object representation of a Frame.

Every backend persists a Frame the same way: a ``metadata.json`` sidecar and a
separate ``body.md`` blob, both UTF-8. This module owns the exact translation
between those bytes and the :class:`~.models.Frame`/:class:`~.models.FrameMetadata`
models so no backend spells ``json.dumps``/``json.loads`` inline. It follows the
module-level codec convention already used by ``history.py``
(``encode_cursor``/``decode_cursor`` plus a typed error) rather than a
class/protocol, because exactly one stored representation exists.

Byte format is a wire contract: metadata is ``json.dumps(..., indent=2)`` with a
single trailing newline, ``ensure_ascii`` on, default separators, and **no**
``sort_keys`` (key order follows pydantic field declaration order). Changing any
of these rewrites every stored sidecar and every S3 ETag, so
``test_frames_codec.py`` pins the format.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from pydantic import ValidationError

from .models import (
    FRAME_METADATA_SCHEMA_VERSION,
    Frame,
    FrameMetadata,
    Visibility,
)

#: Content types S3 stamps on each stored object. Kept here so the byte format
#: and the type advertised for those bytes are defined in one place.
METADATA_CONTENT_TYPE = "application/json"
BODY_CONTENT_TYPE = "text/markdown; charset=utf-8"

#: How often a frame that is *still* corrupt re-warns when skipped. Fan-out
#: reads (lists, group projections, active sets) hit a corrupt frame on every
#: request; warning each time would bury the signal.
UNDECODABLE_WARN_INTERVAL_SECONDS = 3600.0

logger = logging.getLogger("frames_server.codec")


class FrameCodecError(RuntimeError):
    """Base for errors translating between stored bytes and Frame models."""


class FrameDecodeError(FrameCodecError):
    """Stored Frame bytes could not be decoded into a valid model.

    Wraps the underlying ``json.JSONDecodeError``, ``UnicodeDecodeError``,
    ``KeyError`` (a sidecar with no ``id``), or pydantic ``ValidationError``;
    the original is preserved as ``__cause__``. ``TypeError`` is deliberately
    *not* wrapped: it signals a code defect, not bad stored data, and must not
    be skipped as a corrupt frame.

    Deliberately **not** a ``FrameNotFoundError``: the object exists but is
    unreadable, so this must surface as a 500, never a 404.
    """

    def __init__(self, message: str, *, frame_id: str | None = None):
        super().__init__(message)
        self.frame_id = frame_id


def normalize_metadata(metadata: dict) -> dict:
    """Apply backward-compatible defaults to persisted Frame metadata.

    Legacy records carry a single ``owner`` field; migrate it to ``owners`` and
    ``created_by`` and drop the now-unknown key (``extra="forbid"`` would reject
    a leftover ``owner``). New governance fields default conservatively so
    migrated Frames stay owner-only until an owner publishes them.

    Also repairs the reader/visibility invariant on read: a record persisted with
    a non-empty ``readers`` list under an ``internal``/``public`` visibility (e.g.
    written under the earlier "readers restrict internal" semantics) is coerced
    to ``private`` here, so it can never *widen* to whole-tenant/cross-tenant
    access once ``can_read`` stops consulting readers on the internal/public
    branches. Readers only ever apply to ``private`` (Spec 1 §3.3).
    """

    metadata.setdefault("schema_version", FRAME_METADATA_SCHEMA_VERSION)
    metadata.setdefault("org_id", "dev-org")
    metadata.setdefault("workspace_id", "default")
    metadata.setdefault("name", metadata["id"])
    if "owner" in metadata:
        owner = metadata.pop("owner")
        if "owners" not in metadata:
            metadata["owners"] = [owner]
        metadata.setdefault("created_by", owner)
    owners = metadata.get("owners")
    # A non-list ``owners`` is corrupt data; leave it for validation to reject
    # rather than indexing into it here.
    metadata.setdefault("created_by", owners[0] if isinstance(owners, list) and owners else "")
    metadata.setdefault("description", "")
    metadata.setdefault("visibility", Visibility.private.value)
    metadata.setdefault("published", False)
    metadata.setdefault("readers", [])
    metadata.setdefault("group_ids", [])
    # Reader/visibility invariant: non-empty readers ⟹ private. Repairs legacy
    # contradictory records so they never widen access on read.
    if metadata["readers"]:
        metadata["visibility"] = Visibility.private.value
    return metadata


def encode_metadata(frame: FrameMetadata) -> bytes:
    """Serialize Frame metadata to its stored ``metadata.json`` bytes.

    Excludes the body (stored separately). The exact shape — ``indent=2``,
    trailing newline, ``ensure_ascii`` on, no ``sort_keys`` — is the stored-format
    contract; see the module docstring.
    """

    payload = json.dumps(frame.model_dump(mode="json", exclude={"body"}), indent=2)
    return f"{payload}\n".encode("utf-8")


def encode_body(frame: Frame) -> bytes:
    """Serialize a Frame's Markdown body to its stored ``body.md`` bytes."""

    return frame.body.encode("utf-8")


def _decode_sidecar(data: bytes, frame_id: str | None) -> dict:
    """Decode stored metadata bytes to a normalized dict: the gate both decoders share.

    Raises :class:`FrameDecodeError` (with the cause preserved) on invalid
    UTF-8, invalid JSON, a non-object top level, a sidecar missing ``id``, or a
    sidecar carrying a ``body`` key. The body lives in its own blob; a ``body``
    in the sidecar would otherwise clash with it in ``decode_frame`` while
    ``decode_metadata`` rejected it, so listing and GET would disagree.
    """

    try:
        metadata = json.loads(data)
        if not isinstance(metadata, dict):
            raise FrameDecodeError(
                "Stored frame metadata is not a JSON object",
                frame_id=frame_id,
            )
        if "body" in metadata:
            raise FrameDecodeError(
                "Stored frame metadata must not carry a body",
                frame_id=frame_id,
            )
        return normalize_metadata(metadata)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
        raise FrameDecodeError(
            "Stored frame metadata could not be decoded",
            frame_id=frame_id,
        ) from exc


def decode_metadata(data: bytes, *, frame_id: str | None = None) -> FrameMetadata:
    """Decode stored ``metadata.json`` bytes into a :class:`FrameMetadata`.

    Raises :class:`FrameDecodeError` if the bytes are not valid, model-conforming
    metadata; the original error is preserved as ``__cause__``.
    """

    metadata = _decode_sidecar(data, frame_id)
    try:
        return FrameMetadata.model_validate(metadata)
    except ValidationError as exc:
        raise FrameDecodeError(
            "Stored frame metadata could not be decoded",
            frame_id=frame_id,
        ) from exc


def decode_frame(metadata: bytes, body: bytes, *, frame_id: str | None = None) -> Frame:
    """Decode stored metadata + body bytes into a complete :class:`Frame`.

    Raises :class:`FrameDecodeError` if either the metadata or the body is not
    valid, model-conforming Frame content; the original error is preserved as
    ``__cause__``.
    """

    md = _decode_sidecar(metadata, frame_id)
    try:
        # Validated once, from a dict: never ``**`` kwargs, which raise a raw
        # TypeError on a clashing key before validation runs.
        return Frame.model_validate({**md, "body": body.decode("utf-8")})
    except (UnicodeDecodeError, ValidationError) as exc:
        raise FrameDecodeError(
            "Stored frame could not be decoded",
            frame_id=frame_id,
        ) from exc


class UndecodableFrameLog:
    """The one place a fan-out read reports a corrupt frame it skipped.

    Lists, group projections, and active-frame reads all skip an undecodable
    frame rather than failing the whole response. They report the skip here so
    the wording and the rate limit cannot drift between call sites: the first
    skip of a frame warns with the cause attached, repeats within
    ``UNDECODABLE_WARN_INTERVAL_SECONDS`` log at DEBUG, and a frame that is
    still corrupt after the interval warns again.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_warned: dict[str | None, float] = {}
        self._lock = threading.Lock()

    def skipped(self, exc: FrameDecodeError, *, context: str) -> None:
        now = self._clock()
        with self._lock:
            last = self._last_warned.get(exc.frame_id)
            warn = last is None or now - last >= UNDECODABLE_WARN_INTERVAL_SECONDS
            if warn:
                self._last_warned[exc.frame_id] = now
        if warn:
            logger.warning(
                "Frame %s could not be decoded; skipped in %s",
                exc.frame_id,
                context,
                exc_info=exc,
            )
        else:
            logger.debug("Frame %s could not be decoded; skipped in %s", exc.frame_id, context)

    def clear(self) -> None:
        with self._lock:
            self._last_warned.clear()


undecodable_frame_log = UndecodableFrameLog()
