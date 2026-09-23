from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from collab_hub_api.frames.codec import (
    FrameDecodeError,
    decode_frame,
    decode_metadata,
    encode_body,
    encode_metadata,
    normalize_metadata,
)
from collab_hub_api.frames.models import (
    FRAME_METADATA_SCHEMA_VERSION,
    Frame,
    FrameMetadata,
    Visibility,
)


def make_frame(**overrides) -> Frame:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    kwargs = {
        "schema_version": FRAME_METADATA_SCHEMA_VERSION,
        "id": "0" * 32,
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Frame",
        "created_by": "alice",
        "owners": ["alice"],
        "tags": ["sales"],
        "body": "hello world",
        "token_estimate": 3,
        "suggestions": [],
        "created_at": now,
        "updated_at": now,
    }
    kwargs.update(overrides)
    return Frame(**kwargs)


# --- Round-trips -------------------------------------------------------------


def test_decode_frame_round_trips_encode():
    frame = make_frame()
    decoded = decode_frame(encode_metadata(frame), encode_body(frame))
    assert decoded == frame


def test_decode_metadata_round_trips_encode():
    frame = make_frame()
    decoded = decode_metadata(encode_metadata(frame))
    assert decoded == FrameMetadata(**frame.model_dump(exclude={"body"}))


# --- Format pin (stored-format regression guard) -----------------------------


def test_encode_metadata_pins_stored_byte_format():
    frame = make_frame()
    expected = (
        json.dumps(frame.model_dump(mode="json", exclude={"body"}), indent=2) + "\n"
    ).encode("utf-8")
    assert encode_metadata(frame) == expected


def test_encode_metadata_is_deterministic():
    frame = make_frame()
    assert encode_metadata(frame) == encode_metadata(frame)


def test_encode_metadata_keeps_ensure_ascii_escaping():
    # Non-ASCII must be \u-escaped: default ensure_ascii=True is part of the
    # stored contract, and changing it would rewrite every existing sidecar.
    frame = make_frame(name="Café Playbook")
    data = encode_metadata(frame)
    assert b"Caf\\u00e9" in data
    assert "Café".encode("utf-8") not in data
    # And it still decodes back to the original name.
    assert decode_metadata(data).name == "Café Playbook"


def test_encode_body_is_utf8_bytes():
    frame = make_frame(body="café ☕")
    assert encode_body(frame) == "café ☕".encode("utf-8")


# --- Legacy normalization via the codec --------------------------------------


def test_decode_migrates_legacy_owner_scalar():
    legacy = {
        "id": "0" * 32,
        "owner": "legacy-user",
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(legacy).encode("utf-8"))
    assert metadata.owners == ["legacy-user"]
    assert metadata.created_by == "legacy-user"


def test_decode_defaults_missing_governance_fields():
    legacy = {
        "id": "0" * 32,
        "owners": ["alice"],
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(legacy).encode("utf-8"))
    assert metadata.schema_version == FRAME_METADATA_SCHEMA_VERSION
    assert metadata.org_id == "dev-org"
    assert metadata.workspace_id == "default"
    assert metadata.name == "0" * 32  # defaults to id


def test_decode_repairs_reader_visibility_invariant():
    # internal + non-empty readers must be coerced to private on read so it can
    # never widen to whole-tenant access (Spec 1 §3.3).
    record = {
        "id": "1" * 32,
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Legacy Restricted",
        "created_by": "alice",
        "owners": ["alice"],
        "visibility": "internal",
        "readers": ["bob"],
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(record).encode("utf-8"))
    assert metadata.visibility == Visibility.private
    assert metadata.readers == ["bob"]


def test_normalize_metadata_mutates_in_place():
    # Callers (and identity_inventory's mirror) rely on in-place mutation.
    metadata = {
        "id": "0" * 32,
        "owner": "legacy-user",
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    returned = normalize_metadata(metadata)
    assert returned is metadata
    assert metadata["owners"] == ["legacy-user"]
    assert "owner" not in metadata


# --- Error cases -------------------------------------------------------------


def _valid_metadata_bytes() -> bytes:
    return encode_metadata(make_frame())


def test_decode_metadata_wraps_invalid_json():
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"{not json", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_decode_metadata_wraps_undecodable_bytes():
    # json.loads does its own encoding detection, so these bytes surface as a
    # JSONDecodeError rather than a UnicodeDecodeError; either way the codec
    # wraps it instead of leaking a raw decode error. (The UnicodeDecodeError
    # path is exercised directly by the body decode below.)
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"\xff\xfe", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert exc_info.value.__cause__ is not None


def test_decode_metadata_rejects_top_level_array():
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"[]", frame_id="abc")
    # An explicit FrameDecodeError, not a raw TypeError from Frame(**[]).
    assert exc_info.value.frame_id == "abc"


def test_decode_metadata_wraps_missing_id():
    data = json.dumps({"owners": ["alice"]}).encode("utf-8")
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(data)
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_decode_metadata_wraps_unknown_extra_key():
    record = json.loads(_valid_metadata_bytes())
    record["surprise"] = "value"  # extra="forbid" rejects this
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(json.dumps(record).encode("utf-8"))
    assert exc_info.value.__cause__ is not None


def test_decode_metadata_wraps_bad_field_value():
    record = json.loads(_valid_metadata_bytes())
    record["tags"] = ["Bad Tag!"]  # fails tag pattern validation
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(json.dumps(record).encode("utf-8"))
    assert exc_info.value.__cause__ is not None


def test_decode_frame_wraps_empty_body():
    # body has min_length=1, so an empty body.md is a decode error, not a 404.
    frame = make_frame()
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_frame(encode_metadata(frame), b"", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert exc_info.value.__cause__ is not None


def test_decode_frame_wraps_invalid_body_utf8():
    frame = make_frame()
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_frame(encode_metadata(frame), b"\xff\xfe")
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
