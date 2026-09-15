"""Reconciliation indexer: how Cogs get from a registry into the catalog (issue #84).

One sweep, per configured source:

1. enumerate repositories and artifacts through the source adapter;
2. skip digests already in the catalog with the same tag set; a changed tag
   set (or a digest that was marked removed and is back) is written without
   refetching anything;
3. for a new digest fetch the manifest, select ``COG.md`` and the profile
   file it names, read the card, and insert. A manifest with no ``COG.md``
   is recorded as *non-Cog* with a reason so a repository full of images is
   not re-read every cycle;
4. mark this source's rows whose digest is no longer present as
   ``removed_at = now()`` -- never delete;
5. record per-artifact failures in ``read_errors`` and carry on; log a
   summary and export it as metrics.

Two things keep a sweep from doing damage:

- **Single flight.** A sweep runs under the store's sweep lock (a
  session-level Postgres advisory lock on the shared pool), so replicas
  starting together do not double-index; the loser logs and waits for the
  next interval.
- **Removal needs a complete picture.** Rows are only marked removed for a
  source whose enumeration fully succeeded this sweep. A registry that
  answers ``list_repositories`` with an error, or one repository's listing
  that fails, or an enumeration cut short by the per-source bound, leaves
  that source's removal step skipped -- a transient outage must not mark a
  whole catalog gone.

The store is synchronous psycopg; every store call here goes through
``asyncio.to_thread`` so the event loop is never blocked on the database, and
every registry call is the async OCI client. Credentials never appear in
anything this module logs or stores: failures are recorded by exception class
and message, and the OCI client's messages carry neither URLs nor headers.

Targeted entry points for the webhook receiver (#86): :meth:`CogIndexer.reindex`
for one ``(source_id, repository, digest)`` and :meth:`CogIndexer.mark_removed`
for a delete. Neither takes the sweep lock -- both are idempotent single-row
writes, and a sweep running at the same time converges to the same state.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime

from ..frames.observability import COG_INDEX_ARTIFACTS, COG_INDEX_SWEEP_DURATION, COG_INDEX_SWEEPS
from .bundle import COG_ENTRY_FILE, PROG_KIND, CogCard, read_cog_bundle
from .catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    CogCatalogStore,
    KnownArtifact,
    card_search_fields,
)
from .frontmatter import read_cog_document
from .oci import (
    DEFAULT_MAX_BUNDLE_FILE_BYTES,
    LOCKFILE_TITLE,
    Manifest,
    OCIError,
    OCINotFound,
    select_bundle_layers,
)
from .profile import PIXI_MANIFEST, PROFILE_PARSED
from .registry import ArtifactRef, RegistrySource, RegistrySourceError

logger = logging.getLogger("frames_server.cogs.indexer")

DEFAULT_INTERVAL_SECONDS = 300
MAX_ARTIFACTS_PER_SOURCE = 10_000
"""Upper bound on artifacts one sweep will consider for one source.

A registry is somebody else's system: this is what stops a runaway or hostile
one from turning a sweep into an unbounded loop. Past the bound the rest of
the source is left for the next sweep and its removal step is skipped, since
what was not enumerated cannot be declared gone.
"""
MAX_TITLES_IN_REASON = 8
"""How many layer titles a non-Cog reason names before it says "and N more"."""

# Outcome labels for the per-artifact counter. Bounded by construction; the
# summary below carries one field per label.
OUTCOME_INDEXED = "indexed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_RETAGGED = "retagged"
OUTCOME_NON_COG = "non_cog"
OUTCOME_FAILED = "failed"
OUTCOME_REMOVED = "removed"


@dataclass
class SweepSummary:
    """What one sweep did. Logged at the end and exported as counters."""

    indexed: int = 0
    skipped: int = 0
    retagged: int = 0
    non_cog: int = 0
    failed: int = 0
    removed: int = 0
    sources: int = 0
    sources_failed: int = 0
    """Sources whose enumeration did not complete; their removal step was skipped."""
    locked_out: bool = False
    """Another replica held the sweep lock; nothing was done."""
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    """Source-level (not per-artifact) failure messages, bounded to one per source."""

    def as_log_fields(self) -> dict[str, object]:
        fields = asdict(self)
        fields["errors"] = len(self.errors)
        return fields


@dataclass
class _Enumeration:
    """One source's artifacts as seen this sweep, and whether the picture is complete."""

    present: dict[str, list[ArtifactRef]] = field(default_factory=dict)
    complete: bool = True
    error: str | None = None


class CogIndexer:
    """The reconciliation loop over a set of registry sources and one catalog store."""

    def __init__(
        self,
        store: CogCatalogStore,
        sources: Sequence[RegistrySource],
        *,
        max_artifacts_per_source: int = MAX_ARTIFACTS_PER_SOURCE,
        max_bytes_per_file: int = DEFAULT_MAX_BUNDLE_FILE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._sources = list(sources)
        self._max_artifacts = max_artifacts_per_source
        self._max_bytes_per_file = max_bytes_per_file
        self._clock = clock
        self.last_summary: SweepSummary | None = None

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources)

    # -- the sweep ------------------------------------------------------------

    async def sweep(self) -> SweepSummary:
        """Reconcile every source once, under the sweep lock. Never raises for a per-artifact or per-source failure."""

        started = self._clock()
        summary = SweepSummary(sources=len(self._sources))
        # The lock is taken and released on a worker thread: it is a blocking
        # database call, and the connection it occupies stays checked out for
        # the whole sweep (see the store's sweep_lock contract).
        lock = self._store.sweep_lock()
        acquired = await asyncio.to_thread(lock.__enter__)
        try:
            if not acquired:
                summary.locked_out = True
                COG_INDEX_SWEEPS.labels(result="locked_out").inc()
                logger.info("cog_index_sweep_skipped", extra={"reason": "another replica holds the sweep lock"})
                return summary
            for source in self._sources:
                await self._sweep_source(source, summary)
            COG_INDEX_SWEEPS.labels(result="completed").inc()
        except BaseException:
            COG_INDEX_SWEEPS.labels(result="failed").inc()
            raise
        finally:
            await asyncio.to_thread(lock.__exit__, None, None, None)
            summary.duration_seconds = self._clock() - started
            COG_INDEX_SWEEP_DURATION.observe(summary.duration_seconds)
            self.last_summary = summary
            logger.info("cog_index_sweep", extra=summary.as_log_fields())
        return summary

    async def _sweep_source(self, source: RegistrySource, summary: SweepSummary) -> None:
        enumeration = await self._enumerate(source)
        if enumeration.error is not None:
            summary.sources_failed += 1
            summary.errors.append(f"{source.id}: {enumeration.error}")
            logger.warning("cog_index_source_failed", extra={"source": source.id, "reason": enumeration.error})
            if not enumeration.present:
                return

        known = {(row.repository, row.digest): row for row in await asyncio.to_thread(self._store.known, source.id)}
        for repository, artifacts in enumeration.present.items():
            for artifact in artifacts:
                outcome = await self._reconcile(source, repository, artifact, known.get((repository, artifact.digest)))
                _count(summary, outcome)

        if not enumeration.complete:
            # Skipping removal is the whole point of tracking completeness:
            # what was not enumerated cannot be declared gone.
            if enumeration.error is None:
                summary.sources_failed += 1
                summary.errors.append(f"{source.id}: enumeration incomplete; removal step skipped")
            return
        present = {repo: [artifact.digest for artifact in artifacts] for repo, artifacts in enumeration.present.items()}
        removed = await asyncio.to_thread(self._store.mark_removed, source.id, present)
        summary.removed += removed
        if removed:
            COG_INDEX_ARTIFACTS.labels(outcome=OUTCOME_REMOVED).inc(removed)

    async def _enumerate(self, source: RegistrySource) -> _Enumeration:
        result = _Enumeration()
        try:
            repositories = await source.list_repositories()
        except (RegistrySourceError, OCIError) as exc:
            result.complete = False
            result.error = f"list_repositories: {_describe(exc)}"
            return result
        seen = 0
        for repository in repositories:
            try:
                artifacts = await source.list_artifacts(repository)
            except OCINotFound:
                # A configured repository that does not exist (yet) has no
                # artifacts; that is an answer, not a failure, and its rows
                # -- if it ever had any -- are correctly marked removed.
                artifacts = []
            except (RegistrySourceError, OCIError) as exc:
                result.complete = False
                result.error = f"list_artifacts {repository}: {_describe(exc)}"
                continue
            if seen + len(artifacts) > self._max_artifacts:
                keep = max(0, self._max_artifacts - seen)
                logger.warning(
                    "cog_index_source_bounded",
                    extra={"source": source.id, "repository": repository, "bound": self._max_artifacts},
                )
                result.present[repository] = artifacts[:keep]
                result.complete = False
                break
            result.present[repository] = artifacts
            seen += len(artifacts)
        return result

    async def _reconcile(
        self,
        source: RegistrySource,
        repository: str,
        artifact: ArtifactRef,
        known: KnownArtifact | None,
    ) -> str:
        tags = tuple(sorted(set(artifact.tags)))
        if known is not None and known.status != STATUS_FAILED:
            if known.tags == tags and not known.removed:
                return OUTCOME_SKIPPED
            # Tag change, or a digest that is back after being marked removed:
            # the card is the digest's and cannot have changed. Write the tags
            # (which also clears removed_at) and move on without a fetch.
            await asyncio.to_thread(
                self._store.update_tags, source.id, repository, artifact.digest, tags, pushed_at=artifact.pushed_at
            )
            return OUTCOME_RETAGGED
        row = await self._read_artifact(source, repository, artifact)
        await asyncio.to_thread(self._store.upsert, row)
        if row.status == STATUS_INDEXED:
            return OUTCOME_INDEXED
        if row.status == STATUS_NON_COG:
            return OUTCOME_NON_COG
        return OUTCOME_FAILED

    # -- targeted entry points (webhook receiver, #86) --------------------------

    async def reindex(
        self,
        source_id: str,
        repository: str,
        digest: str,
        *,
        tags: Sequence[str] = (),
        pushed_at: datetime | None = None,
    ) -> CogArtifact:
        """Fetch and (re)write one artifact regardless of what the catalog holds.

        For a push event. The row is written whether the read succeeds
        (``indexed``/``non_cog``) or not (``failed``); the returned row is what
        was stored. Raises ``KeyError`` for an unknown source id.
        """

        source = self._source(source_id)
        ref = ArtifactRef(digest=digest, tags=tuple(sorted(set(tags))), pushed_at=pushed_at)
        row = await self._read_artifact(source, repository, ref)
        await asyncio.to_thread(self._store.upsert, row)
        return row

    async def mark_removed(self, source_id: str, repository: str, digest: str) -> bool:
        """Mark one artifact removed (a delete event). Returns whether a present row was marked."""

        self._source(source_id)
        marked = await asyncio.to_thread(self._store.mark_removed_one, source_id, repository, digest)
        if marked:
            COG_INDEX_ARTIFACTS.labels(outcome=OUTCOME_REMOVED).inc()
        return marked

    def _source(self, source_id: str) -> RegistrySource:
        for source in self._sources:
            if source.id == source_id:
                return source
        raise KeyError(f"no registry source with id {source_id!r}")

    # -- reading one artifact ---------------------------------------------------

    async def _read_artifact(self, source: RegistrySource, repository: str, artifact: ArtifactRef) -> CogArtifact:
        """Turn one enumerated artifact into the row to store. Never raises for a registry or reader failure."""

        base = CogArtifact(
            source_id=source.id,
            host=source.host,
            repository=repository,
            digest=artifact.digest,
            status=STATUS_FAILED,
            tags=tuple(sorted(set(artifact.tags))),
            pushed_at=artifact.pushed_at,
            manifest_media_type=artifact.media_type,
        )
        client = source.oci()
        try:
            manifest = await client.get_manifest(repository, artifact.digest)
            base = _with(base, manifest_media_type=manifest.media_type or artifact.media_type)
            if manifest.layer_by_title(COG_ENTRY_FILE) is None:
                return await self._read_without_entry(client, repository, manifest, base)
            files = await self._fetch_cog_files(client, repository, manifest)
        except OCIError as exc:
            # Recorded, not raised: one broken artifact must not stop a sweep.
            # The class name is the diagnosis; the message never carries a URL
            # or a header (the OCI client's contract).
            return _with(base, status=STATUS_FAILED, read_errors=(f"fetch: {_describe(exc)}",))
        card = read_cog_bundle(files, bundle_paths=_titles(manifest))
        return _indexed(base, card)

    async def _fetch_cog_files(self, client, repository: str, manifest: Manifest) -> dict[str, bytes]:
        """``COG.md`` first, then the profile file its frontmatter names (plus ``pixi.toml`` for tasks).

        The manifest pointer is only known after ``COG.md`` is parsed, so this
        is two rounds of fetching rather than one guess at a file name. Never
        the lockfile: ``select_bundle_layers`` drops it even when named.
        """

        entry_layer = manifest.layer_by_title(COG_ENTRY_FILE)
        assert entry_layer is not None
        entry = await client.get_blob(repository, entry_layer, max_bytes=self._max_bytes_per_file)
        files = {COG_ENTRY_FILE: entry}
        pointer = read_cog_document(entry).manifest_path
        layers = select_bundle_layers(manifest, manifest_file=pointer)
        for title, descriptor in layers.items():
            if title in files or title == LOCKFILE_TITLE:
                continue
            files[title] = await client.get_blob(repository, descriptor, max_bytes=self._max_bytes_per_file)
        return files

    async def _read_without_entry(self, client, repository: str, manifest: Manifest, base: CogArtifact) -> CogArtifact:
        """No ``COG.md``: a Prog if ``pixi.toml`` declares a capability, otherwise a non-Cog with a reason."""

        pixi = manifest.layer_by_title(PIXI_MANIFEST)
        if pixi is None:
            return _with(
                base,
                status=STATUS_NON_COG,
                read_errors=(
                    f"manifest carries no {COG_ENTRY_FILE} or {PIXI_MANIFEST} layer; {_layer_summary(manifest)}",
                ),
            )
        files = {PIXI_MANIFEST: await client.get_blob(repository, pixi, max_bytes=self._max_bytes_per_file)}
        card = read_cog_bundle(files, bundle_paths=_titles(manifest))
        if card.kind == PROG_KIND and card.profile_status == PROFILE_PARSED:
            return _indexed(base, card)
        return _with(
            base,
            status=STATUS_NON_COG,
            read_errors=tuple(card.errors) or (f"manifest carries no {COG_ENTRY_FILE} layer",),
        )

    # -- the loop -----------------------------------------------------------------

    async def run(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        run_on_startup: bool = True,
        jitter: float = 0.1,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Sweep on startup (unless told not to) and then every ``interval_seconds``, jittered, until cancelled.

        A sweep that raises (a store outage, a bug) is logged and the loop
        continues: the next interval retries. Cancellation propagates.
        ``jitter`` is a fraction of the interval added or removed at random so
        replicas that start together drift apart instead of contending for
        the lock at the same instant every cycle.
        """

        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        first = True
        while stop is None or not stop.is_set():
            if not first or run_on_startup:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("cog_index_sweep_failed")
            first = False
            delay = interval_seconds * (1 + random.uniform(-jitter, jitter))  # noqa: S311 - scheduling jitter
            if stop is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue


# -- helpers ------------------------------------------------------------------


def _count(summary: SweepSummary, outcome: str) -> None:
    setattr(summary, outcome, getattr(summary, outcome) + 1)
    COG_INDEX_ARTIFACTS.labels(outcome=outcome).inc()


def _with(artifact: CogArtifact, **changes) -> CogArtifact:
    return replace(artifact, **changes)


def _indexed(base: CogArtifact, card: CogCard) -> CogArtifact:
    document = card.to_dict()
    return _with(
        base,
        status=STATUS_INDEXED,
        card=document,
        read_errors=tuple(card.errors),
        **card_search_fields(document),
    )


def _titles(manifest: Manifest) -> list[str]:
    return [layer.title for layer in manifest.layers if layer.title]


def _layer_summary(manifest: Manifest) -> str:
    titles = _titles(manifest)
    if not titles:
        return f"{len(manifest.layers)} layer(s), none titled"
    shown = ", ".join(titles[:MAX_TITLES_IN_REASON])
    more = len(titles) - MAX_TITLES_IN_REASON
    return f"layers: {shown}" + (f" and {more} more" if more > 0 else "")


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
