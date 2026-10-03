"""Serving pulls through the Hub (issue #179): what may be pulled, and from where.

The ``/v2/`` router (:mod:`..routers.registry`) speaks the OCI Distribution
read API; this module is the part behind it that knows the catalog and the
registry sources. It answers three questions and nothing else:

- :meth:`CogRegistryFront.manifest` -- the manifest a reference names;
- :meth:`CogRegistryFront.blob` -- where a blob can be streamed from, and
  how large it is;
- :meth:`CogRegistryFront.tags` -- a repository's tags.

**Only what the catalog holds is served.** Every answer starts from
:meth:`~.catalog.CogCatalogStore.list_pullable`: the present, indexed Cog
artifacts of one repository path. Nothing is passed through to a backing
registry on a client's say-so, so a repository, tag or digest the catalog
does not list is a 404 whether or not the registry holds it.

**Repository names are the backing repository paths**, unchanged, and are
*not* qualified by source: a client addresses ``<hub>/<repository>@<digest>``
and the Hub finds the source holding it. Two sources carrying the same path
are one repository here -- digests are content, so the same digest in both is
the same artifact, served from whichever answers; a tag both carry resolves
to the newest push, as the catalog orders them.

**Tags come from the catalog**, never from a live listing: a tag exists here
exactly when an indexed, present row carries it, and resolves to that row's
digest. A tag pushed since the last sweep is not served until it is indexed.

**Blobs are reachable only through an indexed manifest.** A blob request
names a repository and a digest and nothing else, so the Hub establishes
that some pullable artifact of that repository references the digest before
it opens a stream. It does so from the manifests themselves: the *reach* of
an artifact is its manifest's config and layer digests (and, for an index,
its child manifests and theirs), read once from the source, verified, and
kept in a bounded in-process cache keyed by content digest -- content never
changes under a digest, so an entry is never stale. A client that pulls the
manifest first, as every client does, warms that entry for its blob
requests on the same replica; a cold replica reads the repository's pullable
manifests (newest first, at most
:data:`~.catalog.MAX_PULLABLE_ROWS`) until it finds the digest. The
descriptor's ``size`` comes with it, which is how a blob response declares
its length -- and how an oversized blob is refused -- without asking the
registry.

**Multi-platform indexes** are served as stored: the index itself, then each
child manifest by digest, then their blobs. At most
:data:`MAX_SERVED_INDEX_CHILDREN` children are followed, and an index nested
inside an index is not.

Failures are :class:`ServeError` subclasses whose messages are written for
the client: they name the Hub repository and the digest and never a backing
host, URL or credential. Upstream response bodies are never read into them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool

from .catalog import CogCatalogStore, PullableArtifact
from .oci import (
    MEDIA_TYPE_OCI_MANIFEST,
    Manifest,
    OCIError,
    OCINotFound,
    OCITransportError,
    index_children,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)
from .registry import RegistrySource, is_repository_path
from .registry_credentials import RegistryCredentialStore

logger = logging.getLogger("frames_server.cogs.serving")

MAX_SERVED_INDEX_CHILDREN = 32
"""Children of one index the Hub will follow; an index listing more is served without them."""

REACH_CACHE_ENTRIES = 2048
"""Artifacts whose reach is kept in memory, least recently used first out."""

MAX_CACHED_MANIFEST_BYTES = 16 * 1024
"""Manifest bodies up to this size are kept with the reach; larger ones are re-read on demand.

Bounds the cache at ``REACH_CACHE_ENTRIES * (1 + children) * 16 KiB`` in the
worst case; a Cog manifest is one or two kilobytes.
"""


class ServeError(Exception):
    """Base for every refusal this module reports. The message is safe to send to a client."""


class RepositoryUnknown(ServeError):
    """The catalog holds no pullable artifact under this repository path."""


class ManifestUnknown(ServeError):
    """The reference names nothing pullable in this repository."""


class BlobUnknown(ServeError):
    """No pullable manifest of this repository references this blob."""


class BlobTooLarge(ServeError):
    """The blob is larger than ``cogs.serve.max_blob_bytes``."""


class UpstreamUnavailable(ServeError):
    """The source holding the artifact could not serve it right now."""


@dataclass(frozen=True)
class ServedManifest:
    digest: str
    media_type: str
    body: bytes


@dataclass(frozen=True)
class ServedBlob:
    """An open, not-yet-read blob. Drain :attr:`chunks` or call :meth:`aclose`."""

    digest: str
    size: int
    chunks: AsyncGenerator[bytes]

    async def aclose(self) -> None:
        await self.chunks.aclose()


@dataclass(frozen=True)
class _ReachManifest:
    media_type: str
    size: int
    body: bytes | None
    """``None`` when the body is over :data:`MAX_CACHED_MANIFEST_BYTES` and must be re-read."""


@dataclass(frozen=True)
class _Reach:
    """Everything one pullable artifact makes reachable: its manifests and their blobs."""

    manifests: Mapping[str, _ReachManifest]
    blobs: Mapping[str, int]


_ReachKey = tuple[str, str, str]
"""(source id, repository, artifact digest)."""


def _content_type(manifest: Manifest) -> str:
    return manifest.media_type or MEDIA_TYPE_OCI_MANIFEST


class CogRegistryFront:
    """The catalog and the sources, as one read-only registry. One per app; safe across requests."""

    def __init__(
        self,
        store: CogCatalogStore,
        sources: Sequence[RegistrySource],
        *,
        max_blob_bytes: int,
    ) -> None:
        self._store = store
        self._sources = {source.id: source for source in sources}
        self._max_blob_bytes = max_blob_bytes
        self._reach_cache: OrderedDict[_ReachKey, _Reach] = OrderedDict()
        # Single flight per artifact: a burst of blob requests on a cold
        # replica reads each manifest once, not once per request.
        self._inflight: dict[_ReachKey, asyncio.Future[_Reach]] = {}

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources.values())

    # -- the three reads ------------------------------------------------------

    async def tags(self, repository: str) -> list[str]:
        rows = await self._pullable(repository)
        return sorted({tag for row in rows for tag in row.tags})

    async def manifest(self, repository: str, reference: str) -> ServedManifest:
        rows = await self._pullable(repository)
        if is_tag(reference):
            # Newest first, so the first row carrying the tag is the one it names.
            named = next((row for row in rows if reference in row.tags), None)
            if named is None:
                raise ManifestUnknown(f"manifest {reference} is not known to {repository}")
            digest = named.digest
        elif is_sha256_digest(reference):
            digest = reference
        else:
            raise ManifestUnknown(f"manifest {reference} is not known to {repository}")

        unavailable = False
        candidates = [row for row in rows if row.digest == digest]
        for row in candidates or rows:
            # An artifact's own digest is tried in every source that holds it;
            # any other digest must be a child of some pullable index.
            try:
                reach = await self._reach(row)
            except OCINotFound:
                continue
            except OCIError:
                unavailable = True
                continue
            entry = reach.manifests.get(digest)
            if entry is None:
                continue
            if entry.body is not None:
                return ServedManifest(digest=digest, media_type=entry.media_type, body=entry.body)
            try:
                manifest = await self._source(row).oci().fetch_manifest(repository, digest)
            except OCINotFound:
                continue
            except OCIError as exc:
                self._log_upstream(row, exc)
                unavailable = True
                continue
            return ServedManifest(digest=digest, media_type=_content_type(manifest), body=manifest.raw)
        if unavailable:
            raise UpstreamUnavailable(f"manifest {reference} of {repository} is temporarily unavailable")
        raise ManifestUnknown(f"manifest {reference} is not known to {repository}")

    async def blob_size(self, repository: str, digest: str) -> int:
        """The size of a reachable blob, from the manifest that references it (no registry round trip)."""

        _row, size = await self._locate_blob(repository, digest)
        return size

    async def blob(self, repository: str, digest: str) -> ServedBlob:
        row, size = await self._locate_blob(repository, digest)
        try:
            stream = await self._source(row).oci().open_blob(repository, digest)
        except OCINotFound:
            raise BlobUnknown(f"blob {digest} is not known to {repository}") from None
        except OCIError as exc:
            self._log_upstream(row, exc)
            raise UpstreamUnavailable(f"blob {digest} of {repository} is temporarily unavailable") from None
        declared = stream.content_length
        if declared is not None and declared != size:
            # The registry is about to send something other than what the
            # manifest promised; refuse before a single byte is relayed.
            await stream.aclose()
            logger.warning("cog_serve_blob_size_mismatch", extra={"source_id": row.source_id, "digest": digest})
            raise UpstreamUnavailable(f"blob {digest} of {repository} is temporarily unavailable")
        return ServedBlob(digest=digest, size=size, chunks=stream.iter_verified(max_bytes=size, expected_size=size))

    # -- what is reachable ----------------------------------------------------

    async def _pullable(self, repository: str) -> list[PullableArtifact]:
        if not is_repository_path(repository):
            raise RepositoryUnknown(f"repository {repository} is not known to this registry")
        rows = await run_in_threadpool(self._store.list_pullable, repository)
        # A row whose source is no longer configured cannot be served; it is
        # dropped here rather than failing every request for the repository.
        rows = [row for row in rows if row.source_id in self._sources]
        if not rows:
            raise RepositoryUnknown(f"repository {repository} is not known to this registry")
        return rows

    async def _locate_blob(self, repository: str, digest: str) -> tuple[PullableArtifact, int]:
        if not is_sha256_digest(digest):
            raise BlobUnknown(f"blob {digest} is not known to {repository}")
        rows = await self._pullable(repository)
        # What this replica already knows first: the manifest a client pulled
        # a moment ago answers without touching the registry.
        cold: list[PullableArtifact] = []
        for row in rows:
            reach = self._cached(row)
            if reach is None:
                cold.append(row)
            elif digest in reach.blobs:
                return self._checked(row, repository, digest, reach.blobs[digest])
        unavailable = False
        for row in cold:
            try:
                reach = await self._reach(row)
            except OCINotFound:
                continue
            except OCIError:
                unavailable = True
                continue
            if digest in reach.blobs:
                return self._checked(row, repository, digest, reach.blobs[digest])
        if unavailable:
            # Not "unknown": a manifest that could not be read may be the one
            # that references it, and a 404 would be cached by the client.
            raise UpstreamUnavailable(f"blob {digest} of {repository} is temporarily unavailable")
        raise BlobUnknown(f"blob {digest} is not known to {repository}")

    def _checked(self, row: PullableArtifact, repository: str, digest: str, size: int) -> tuple[PullableArtifact, int]:
        if size > self._max_blob_bytes:
            raise BlobTooLarge(
                f"blob {digest} of {repository} is {size} bytes, over this registry's {self._max_blob_bytes}-byte limit"
            )
        return row, size

    def _source(self, row: PullableArtifact) -> RegistrySource:
        return self._sources[row.source_id]

    def _cached(self, row: PullableArtifact) -> _Reach | None:
        key = (row.source_id, row.repository, row.digest)
        reach = self._reach_cache.get(key)
        if reach is not None:
            self._reach_cache.move_to_end(key)
        return reach

    async def _reach(self, row: PullableArtifact) -> _Reach:
        cached = self._cached(row)
        if cached is not None:
            return cached
        key = (row.source_id, row.repository, row.digest)
        flight = self._inflight.get(key)
        if flight is None:
            flight = self._inflight[key] = asyncio.get_running_loop().create_future()
            try:
                reach = await self._read_reach(row)
            except BaseException as exc:
                if isinstance(exc, OCIError):
                    self._log_upstream(row, exc)
                    flight.set_exception(exc)
                else:
                    # This request went away (a client disconnect cancels it)
                    # or hit a bug. Requests that joined the flight are not
                    # cancelled themselves, so they get an ordinary upstream
                    # failure and the next one reads again.
                    flight.set_exception(OCITransportError("manifest read was interrupted"))
                # Retrieved here so a flight nobody else joined does not
                # report "exception was never retrieved" at collection.
                flight.exception()
                raise
            else:
                self._reach_cache[key] = reach
                while len(self._reach_cache) > REACH_CACHE_ENTRIES:
                    self._reach_cache.popitem(last=False)
                flight.set_result(reach)
                return reach
            finally:
                del self._inflight[key]
        return await asyncio.shield(flight)

    async def _read_reach(self, row: PullableArtifact) -> _Reach:
        client = self._source(row).oci()
        root = await client.fetch_manifest(row.repository, row.digest)
        manifests: dict[str, _ReachManifest] = {row.digest: _kept(root)}
        blobs: dict[str, int] = {}
        _collect_blobs(root, blobs)
        for child in index_children(root)[:MAX_SERVED_INDEX_CHILDREN]:
            try:
                manifest = await client.fetch_manifest(row.repository, child.digest)
            except OCINotFound:
                # An index may list a platform its publisher never pushed.
                continue
            if is_index_manifest(manifest):
                continue
            manifests[child.digest] = _kept(manifest)
            _collect_blobs(manifest, blobs)
        return _Reach(manifests=manifests, blobs=blobs)

    def _log_upstream(self, row: PullableArtifact, exc: OCIError) -> None:
        # The class and the source id only. OCIError messages carry request
        # paths, never hosts; even so the operator's handle on "which
        # registry" is the source id, and that is what is logged.
        logger.warning(
            "cog_serve_upstream_failed",
            extra={"source_id": row.source_id, "repository": row.repository, "error": type(exc).__name__},
        )


@dataclass(frozen=True)
class CogRegistryServing:
    """Everything the ``/v2/`` router and the credential exchange need, built once per app."""

    front: CogRegistryFront
    credentials: RegistryCredentialStore
    host: str
    """``host[:port]`` clients address the Hub registry by; the ``registry`` of an exchanged credential."""
    token_url: str
    """The bearer realm: ``<public url>/v2/token``."""
    credential_ttl_seconds: int
    token_ttl_seconds: int
    max_blob_seconds: int


def _kept(manifest: Manifest) -> _ReachManifest:
    body = manifest.raw if len(manifest.raw) <= MAX_CACHED_MANIFEST_BYTES else None
    return _ReachManifest(media_type=_content_type(manifest), size=len(manifest.raw), body=body)


def _collect_blobs(manifest: Manifest, blobs: dict[str, int]) -> None:
    if manifest.config is not None:
        blobs[manifest.config.digest] = manifest.config.size
    for layer in manifest.layers:
        blobs[layer.digest] = layer.size
