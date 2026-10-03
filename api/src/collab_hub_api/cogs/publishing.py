"""Publishing through the Hub (issue #180): pushes written through to one source.

The ``/v2/`` router speaks the push half of the OCI Distribution API; this
module is what stands behind it. A push is accepted from a client that holds
nothing but a Hub sign-in, checked, and written to the one registry source
marked ``publish: true`` with the Hub's own credential for that source.

**Who may push.** Three things, all checked on *every* push request and
before a byte is forwarded (:meth:`CogPublisher.authorize`):

1. the caller holds the **publish permission** (:class:`PublishPolicy`): a
   role or a user id the deployment's configuration names. Nobody by
   default, and being able to pull never implies it;
2. the repository is one the caller's **organization owns**, or a new one. A
   repository first published through the Hub belongs to the publisher's
   organization, recorded with the first manifest the Hub accepts for it;
   later pushes need membership of that organization. A platform operator
   may push to any;
3. a repository the catalog already knows but that was **not** published
   through the Hub (it has no owner record) accepts pushes from platform
   operators only.

**Uploads** are sessions (:mod:`.publish_store`): the client sees the Hub's
own session id, the backing registry's session URL stays in the database,
and bytes are streamed through -- counted against ``max_blob_bytes``, never
buffered. The backing registry verifies each blob against its digest when
the upload is closed.

**A manifest is validated before it is committed**
(:meth:`CogPublisher.put_manifest`). The blobs it names were just uploaded,
so the Hub reads the bundle with the catalog's own reader, through the
indexer's code path, *before* forwarding the manifest. A bundle the catalog
would not list -- not a Cog, no id, or a reader error -- is refused with the
reader's errors, nothing is written to the registry and nothing is listed.

**An accepted manifest is indexed in the request**: the row the validation
produced is stored through the indexer's lock-less targeted path, and the
authenticated publisher (Hub user and organization) is recorded on it. A
later sweep reconciles the row like any other and leaves the publisher alone.

Failures are :class:`PublishError` subclasses whose messages are written for
the client and name no backing host, URL or credential.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool

from ..frames.orgs import PLATFORM_ROLE_OPERATOR
from .catalog import STATUS_INDEXED, CogCatalogStore
from .deadline import renew as renew_budget
from .indexer import CogIndexer
from .oci import (
    Descriptor,
    Manifest,
    OCIAuthError,
    OCIError,
    OCIProtocolError,
    OCIRejected,
    OCITransportError,
    _parse_manifest,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)
from .publish_store import PublishStore, UploadSession, new_upload_id
from .registry import ArtifactRef, RegistrySource, is_repository_path
from .serving import manifest_blobs

logger = logging.getLogger("frames_server.cogs.publishing")

PUBLISH_ROLES = ("operator", "owner", "member")
"""The roles ``cogs.publish.allowed_roles`` may name: the platform role, then the two organization roles."""


class PublishError(Exception):
    """Base for every refusal this module reports. The message is safe to send to a client."""


class PublishDenied(PublishError):
    """The caller may not push here: no publish permission, or the repository is not theirs."""


class RepositoryInvalid(PublishError):
    """Not a repository name."""


class UploadUnknown(PublishError):
    """No such upload session for this caller and repository (or it expired)."""


class UploadInvalid(PublishError):
    """The upload request does not fit the session: a chunk out of order, a malformed range."""

    def __init__(self, message: str, *, received: int | None = None) -> None:
        super().__init__(message)
        self.received = received


class UploadTooLarge(PublishError):
    """The blob is larger than ``cogs.serve.max_blob_bytes``."""


class DigestInvalid(PublishError):
    """The digest is malformed, or the bytes do not hash to it."""


class ManifestInvalid(PublishError):
    """The manifest was refused; ``errors`` says why, one entry per problem."""

    def __init__(self, message: str, errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.errors = errors or (message,)


class PublishUnavailable(PublishError):
    """The publish source could not take the write right now."""


@dataclass(frozen=True)
class PublishPolicy:
    """Who holds the publish permission. Empty -- the default -- means nobody."""

    allowed_roles: frozenset[str] = frozenset()
    allowed_users: frozenset[str] = frozenset()

    def permits(self, user_id: str, org_role: str | None, platform_role: str | None) -> bool:
        if user_id in self.allowed_users:
            return True
        if platform_role == PLATFORM_ROLE_OPERATOR and "operator" in self.allowed_roles:
            return True
        return org_role in ("owner", "member") and org_role in self.allowed_roles


@dataclass(frozen=True)
class Publisher:
    """The caller of a push, as the Hub resolves them **now**: never as a token remembered them."""

    user_id: str
    org_id: str | None
    org_role: str | None = None
    platform_role: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.platform_role == PLATFORM_ROLE_OPERATOR


class _ManifestInHand:
    """The registry client, answering for one manifest that has not been pushed yet.

    The reader asks its client for the manifest and then for the layers it
    needs. During validation the manifest exists only in this request; the
    layers were just uploaded and are read from the registry, verified
    against their digests like any other read.
    """

    def __init__(self, client, manifest: Manifest) -> None:
        self._client = client
        self._manifest = manifest
        self.unavailable = False

    async def get_manifest(self, repo: str, ref: str) -> Manifest:
        return self._manifest

    async def get_blob(self, repo: str, descriptor: Descriptor | str, *, max_bytes: int) -> bytes:
        try:
            return await self._client.get_blob(repo, descriptor, max_bytes=max_bytes)
        except (OCITransportError, OCIAuthError):
            # The registry could not be asked. That is an outage, not a
            # verdict on the bundle, and must not be reported as one.
            self.unavailable = True
            raise


class _SourceInHand:
    def __init__(self, source: RegistrySource, client: _ManifestInHand) -> None:
        self.id = source.id
        self.host = source.host
        self._client = client

    def oci(self) -> _ManifestInHand:
        return self._client


class CogPublisher:
    """Pushes, written through to the publish source. One per app; holds no per-request state."""

    def __init__(
        self,
        *,
        catalog: CogCatalogStore,
        store: PublishStore,
        source: RegistrySource,
        indexer: CogIndexer,
        policy: PublishPolicy,
        max_blob_bytes: int,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._source = source
        self._indexer = indexer
        self.policy = policy
        self._max_blob_bytes = max_blob_bytes

    @property
    def source_id(self) -> str:
        return self._source.id

    def close(self) -> None:
        self._indexer.close()

    # -- authorization ---------------------------------------------------------

    def authorize(self, publisher: Publisher, repository: str) -> None:
        """Refuse a push this caller may not make to this repository. Blocking: two store reads at most.

        Run on every push request, before anything is sent to the registry.
        """

        if not is_repository_path(repository):
            raise RepositoryInvalid(f"{repository} is not a repository name")
        if not self.policy.permits(publisher.user_id, publisher.org_role, publisher.platform_role):
            raise PublishDenied("this account does not hold the permission to publish Cogs")
        record = self._store.get_repository(repository)
        if record is not None:
            self._require_owner(publisher, record.owner_org_id, repository)
        elif self._catalog.repository_known(repository) and not publisher.is_operator:
            raise PublishDenied(
                f"{repository} already exists and was not published through this Hub; "
                "only a platform operator may publish to it"
            )

    def _require_owner(self, publisher: Publisher, owner_org_id: str | None, repository: str) -> None:
        if publisher.is_operator:
            return
        if publisher.org_id is None or owner_org_id != publisher.org_id:
            raise PublishDenied(f"{repository} belongs to another organization")

    # -- blobs -----------------------------------------------------------------

    async def blob_size(self, repository: str, digest: str) -> int | None:
        """The size of a blob the publish source already holds in this repository, or ``None``.

        For a caller already authorized to push here: lets a client skip an
        upload it does not need. It makes nothing pullable.
        """

        if not is_sha256_digest(digest):
            return None
        try:
            return await self._source.oci().blob_size(repository, digest)
        except OCIError as exc:
            self._log_upstream("blob check", exc)
            return None

    async def start(self, publisher: Publisher, repository: str) -> UploadSession:
        try:
            location = await self._source.oci().start_upload(repository)
        except OCIError as exc:
            raise self._unavailable("upload start", exc) from None
        return await run_in_threadpool(
            lambda: self._store.open_upload(
                upload_id=new_upload_id(),
                user_id=publisher.user_id,
                repository=repository,
                source_id=self._source.id,
                upstream_location=location,
            )
        )

    async def status(self, publisher: Publisher, repository: str, upload_id: str) -> UploadSession:
        session = await run_in_threadpool(
            lambda: self._store.get_upload(upload_id, user_id=publisher.user_id, repository=repository)
        )
        if session is None:
            raise UploadUnknown("blob upload unknown to registry")
        return session

    async def append(
        self,
        publisher: Publisher,
        repository: str,
        upload_id: str,
        content: AsyncIterable[bytes],
        *,
        start: int | None,
        length: int | None,
    ) -> UploadSession:
        """Forward the next chunk. ``start`` is the offset the client claims (from ``Content-Range``), if it said."""

        session = await self.status(publisher, repository, upload_id)
        if start is not None and start != session.received:
            raise UploadInvalid("the chunk does not start where the upload left off", received=session.received)
        self._check_size(session.received + (length or 0))
        counter = _Counted(content, limit=self._max_blob_bytes - session.received)
        try:
            location = await self._source.oci().upload_chunk(
                repository, session.upstream_location, counter.chunks(), offset=session.received, length=length
            )
        except _LimitExceeded:
            await self._abandon(session)
            raise UploadTooLarge(self._too_large()) from None
        except OCIError as exc:
            raise self._write_failed("upload chunk", exc, session) from None
        renew_budget()
        received = session.received + counter.count
        moved = await run_in_threadpool(
            lambda: self._store.advance_upload(
                upload_id, expected_received=session.received, received=received, upstream_location=location
            )
        )
        if not moved:
            # Two requests wrote to one session at once; the registry has
            # bytes this record does not account for. Nothing can be trusted
            # about the session's position any more.
            await self._abandon(session)
            raise UploadInvalid("the upload was written to concurrently and has been cancelled")
        return replace(session, received=received, upstream_location=location)

    async def finish(
        self,
        publisher: Publisher,
        repository: str,
        upload_id: str,
        digest: str,
        content: AsyncIterable[bytes] | None,
        *,
        length: int | None,
    ) -> None:
        """Close an upload as ``digest`` (optionally with its last bytes); the registry verifies the digest."""

        if not is_sha256_digest(digest):
            raise DigestInvalid("the digest must be sha256: followed by 64 lowercase hex digits")
        session = await self.status(publisher, repository, upload_id)
        self._check_size(session.received + (length or 0))
        counter = _Counted(content, limit=self._max_blob_bytes - session.received) if content is not None else None
        try:
            await self._source.oci().finish_upload(
                repository,
                session.upstream_location,
                digest,
                counter.chunks() if counter is not None else None,
                length=length,
            )
        except _LimitExceeded:
            await self._abandon(session)
            raise UploadTooLarge(self._too_large()) from None
        except OCIRejected as exc:
            await self._abandon(session, cancel=exc.status != 404)
            if exc.status == 404:
                raise UploadUnknown("blob upload unknown to registry") from None
            # The registry checked the bytes against the digest and they did not match.
            raise DigestInvalid("the uploaded content does not match the digest") from None
        except OCIError as exc:
            raise self._unavailable("upload finish", exc) from None
        renew_budget()
        await run_in_threadpool(self._store.close_upload, upload_id)

    async def cancel(self, publisher: Publisher, repository: str, upload_id: str) -> None:
        session = await self.status(publisher, repository, upload_id)
        await self._abandon(session)

    async def _abandon(self, session: UploadSession, *, cancel: bool = True) -> None:
        renew_budget()
        if cancel:
            await self._source.oci().cancel_upload(session.repository, session.upstream_location)
        await run_in_threadpool(self._store.close_upload, session.id)

    def _check_size(self, total: int) -> None:
        if total > self._max_blob_bytes:
            raise UploadTooLarge(self._too_large())

    def _too_large(self) -> str:
        return f"the blob is over this registry's {self._max_blob_bytes}-byte limit"

    def _write_failed(self, what: str, exc: OCIError, session: UploadSession) -> PublishError:
        if isinstance(exc, OCIRejected):
            if exc.status == 404:
                return UploadUnknown("blob upload unknown to registry")
            return UploadInvalid("the registry refused the chunk", received=session.received)
        return self._unavailable(what, exc)

    # -- manifests -------------------------------------------------------------

    async def put_manifest(
        self, publisher: Publisher, repository: str, reference: str, body: bytes, content_type: str
    ) -> str:
        """Validate, commit and index one manifest. Returns its digest."""

        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if is_sha256_digest(reference):
            if reference != digest:
                raise DigestInvalid("the manifest does not hash to the digest it was put under")
            tags: tuple[str, ...] = ()
        elif is_tag(reference):
            tags = (reference,)
        else:
            raise ManifestInvalid("the reference is neither a tag nor a sha256 digest")
        try:
            manifest = _parse_manifest(body, digest, content_type_fallback=content_type)
        except OCIProtocolError as exc:
            raise ManifestInvalid(str(exc)) from None
        if is_index_manifest(manifest):
            raise ManifestInvalid("multi-platform indexes are not accepted: a Cog bundle is a single manifest")
        if any(layer.size > self._max_blob_bytes for layer in manifest.layers):
            raise ManifestInvalid(f"a layer is over this registry's {self._max_blob_bytes}-byte limit")

        row = await self._validate(repository, manifest, tags)

        # The repository's owner is settled by the first manifest accepted
        # for it, atomically: of two organizations publishing a new name at
        # once, one owns it and the other is refused here, before the write.
        record = await run_in_threadpool(
            lambda: self._store.claim_repository(
                repository, source_id=self._source.id, owner_org_id=publisher.org_id, created_by=publisher.user_id
            )
        )
        self._require_owner(publisher, record.owner_org_id, repository)

        try:
            await self._source.oci().put_manifest(repository, reference, body, manifest.media_type or content_type)
        except OCIRejected:
            raise ManifestInvalid("the registry refused the manifest") from None
        except OCIError as exc:
            raise self._unavailable("manifest put", exc) from None

        # Committed. Index it now, from the row the validation produced, so
        # the catalog lists it without waiting for a sweep.
        known = await run_in_threadpool(
            lambda: self._catalog.get(digest, source_id=self._source.id, repository=repository)
        )
        if known is not None:
            row = replace(row, tags=tuple(sorted({*row.tags, *known.tags})))
        await self._indexer.record(row)
        await run_in_threadpool(
            lambda: self._catalog.record_publication(
                self._source.id, repository, digest, user_id=publisher.user_id, org_id=publisher.org_id
            )
        )
        await run_in_threadpool(
            self._catalog.record_manifest_blobs, self._source.id, repository, digest, manifest_blobs(manifest)
        )
        logger.info(
            "cog_published",
            extra={"repository": repository, "digest": digest, "cog_id": row.cog_id, "user": publisher.user_id},
        )
        return digest

    async def _validate(self, repository: str, manifest: Manifest, tags: tuple[str, ...]):
        """Read the bundle with the catalog's reader; refuse what the catalog would not list."""

        client = _ManifestInHand(self._source.oci(), manifest)
        artifact = ArtifactRef(digest=manifest.digest, tags=tags, pushed_at=datetime.now(UTC))
        row = await self._indexer.inspect(_SourceInHand(self._source, client), repository, artifact)
        if client.unavailable:
            raise PublishUnavailable("the bundle could not be read back for validation; try again")
        errors = tuple(row.read_errors)
        if row.status != STATUS_INDEXED:
            raise ManifestInvalid("the bundle is not a Cog the catalog can index", errors)
        if errors:
            raise ManifestInvalid("the bundle has errors and would not be listed correctly", errors)
        if not row.cog_id:
            raise ManifestInvalid("the bundle declares no id, so the catalog could not list it")
        return row

    # -- failures --------------------------------------------------------------

    def _unavailable(self, what: str, exc: OCIError) -> PublishUnavailable:
        self._log_upstream(what, exc)
        return PublishUnavailable("the registry is temporarily unable to accept this write")

    def _log_upstream(self, what: str, exc: OCIError) -> None:
        # The source id and the error's class: never its message's host, and
        # never a response body (none is read). A refused credential is an
        # operator's problem and is logged as one.
        level = logging.ERROR if isinstance(exc, OCIAuthError) else logging.WARNING
        logger.log(
            level,
            "cog_publish_upstream_failed",
            extra={"source_id": self._source.id, "step": what, "error": type(exc).__name__},
        )


class _LimitExceeded(Exception):
    """Raised inside a request body stream once it has carried more than the limit allows."""


class _Counted:
    """A request body, counted as it is forwarded and cut off at ``limit`` bytes."""

    def __init__(self, content: AsyncIterable[bytes], *, limit: int) -> None:
        self._content = content
        self._limit = limit
        self.count = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._content:
            self.count += len(chunk)
            if self.count > self._limit:
                raise _LimitExceeded()
            yield chunk
