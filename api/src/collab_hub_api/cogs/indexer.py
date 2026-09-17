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

Three things keep a sweep from doing damage:

- **Single flight, cancellation included.** A sweep runs under the store's
  sweep lock (a session-level Postgres advisory lock on the shared pool), so
  replicas starting together do not double-index; the loser logs and waits
  for the next interval. Store calls run on worker threads, and a worker
  thread cannot be interrupted -- so cancellation *drains*: a cancelled sweep
  first waits out whatever store call is in flight, then releases the lock,
  then propagates. The lock is never released while a write is still running,
  and a cancellation that lands mid-acquisition still gets a matching release.
- **Removal needs a complete picture.** Rows are only marked removed for a
  source whose enumeration fully succeeded this sweep. A registry that
  answers ``list_repositories`` with an error, one repository's listing that
  fails, or an enumeration past the per-source bound leaves that source's
  removal step skipped -- a transient outage must not mark a whole catalog
  gone. The adapters uphold their half: past their own limits they *raise*
  rather than silently truncate, because a truncated list presented as
  complete is exactly what turns a bound into false removals.
- **One artifact cannot poison the sweep.** The whole per-artifact read
  is guarded (any exception, not just the OCI hierarchy -- a client bug must
  cost one row, not the sweep), and a card Postgres cannot store (NUL in a
  string, or oversized) is recorded as ``failed`` with a reason instead of
  aborting. Database *outages* still abort the sweep on purpose: retrying
  per artifact against a dead database would spend the whole budget learning
  the same fact.

**What is and is not kept out of storage and logs.** Cards are the published
bundle's own content, stored verbatim -- that is the contract (parent issue
acceptance: the reader's output, structure preserved), so anything a
publisher writes into ``COG.md`` or the profile lands in the catalog as-is.
The guarantee this module makes is narrower and absolute: *configured
registry credentials* never reach cards, ``read_errors``, or logs.
``read_errors`` and log lines therefore carry exception class names -- plus
the message only for the OCI hierarchy, whose contract is that messages name
neither URLs nor headers -- never raw URLs, tokens, or exception chains.

Fairness under bounds: enumeration is complete (or the source is marked
failed), while *fetching* is budgeted per sweep. Already-known digests cost
nothing, so successive sweeps walk past what earlier sweeps indexed and the
tail of a large registry is reached instead of starved; deferred artifacts
are still part of the present set, so removal stays correct.

Targeted entry points for the webhook receiver (#86): :meth:`CogIndexer.reindex`
for one ``(source_id, repository, digest)`` and :meth:`CogIndexer.mark_removed`
for a delete. Neither takes the sweep lock -- both are idempotent single-row
writes, and a sweep running at the same time converges to the same state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime

from ..frames.observability import COG_INDEX_ARTIFACTS, COG_INDEX_SWEEP_DURATION, COG_INDEX_SWEEPS
from .bundle import COG_ENTRY_FILE, PROG_KIND, CogCard, read_cog_bundle
from .catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    CogCatalogDataError,
    CogCatalogStore,
    KnownArtifact,
    card_search_fields,
    contains_nul,
)
from .frontmatter import read_cog_document
from .oci import (
    DEFAULT_MAX_BUNDLE_FILE_BYTES,
    LOCKFILE_TITLE,
    MEDIA_TYPE_PIXI_LOCK,
    Descriptor,
    Manifest,
    OCIError,
    OCINotFound,
    select_bundle_layers,
)
from .profile import PIXI_MANIFEST, PROFILE_PARSED
from .registry import ArtifactRef, RegistrySource

logger = logging.getLogger("frames_server.cogs.indexer")

DEFAULT_INTERVAL_SECONDS = 300

STORE_WORKER_THREADS = 4
"""Size of the indexer's own store-call thread pool.

Its own, rather than asyncio's default executor, for one reason: the
``concurrent.futures.Future`` a submit returns is owned by this object and is
not tied to the event loop, so the lock hand-off below can wait for a worker
from a plain thread after the loop that started it is gone.
"""

DRAIN_DEADLINE_SECONDS = 30.0
"""How long a cancelled sweep waits for an in-flight worker thread before handing it off.

A worker thread cannot be interrupted, so a cancelled sweep *drains* -- waits
for the thread -- before releasing the lock. But an unresponsive established
connection is not bounded by the pool timeout (which bounds checkout only) or
by ``statement_timeout`` (which the server cannot enforce if the transport is
dead), so the wait itself must have a deadline. Past it the sweep coroutine
stops waiting -- but it does **not** release the lock: ownership of the lock
and of the still-running worker is handed to a daemon **thread**
(:meth:`CogIndexer._hand_off`) that releases only once the worker has actually
finished. A thread, not an asyncio task, because the hand-off has to outlive
the event loop: at shutdown every task is cancelled and the loop closes, and
an asyncio owner would simply stop running -- leaving the lock context object
to be finalized by the garbage collector, whose ``finally`` would unlock while
the write was still in flight. The lock is therefore never released while a
write may still be running; what the deadline bounds is the sweep coroutine,
and through it the lifespan's shutdown wait. Chosen above
:data:`..catalog.SWEEP_STATEMENT_TIMEOUT_SECONDS` so the server's own abort
fires first in every case where the transport still works.

If the process exits first, the hand-off thread is a daemon and dies with it
-- which is safe for the same reason abandoning is safe at all: the advisory
lock is session-scoped, so the server releases it when the connection goes.
"""

INDEXER_SHUTDOWN_TIMEOUT_SECONDS = DRAIN_DEADLINE_SECONDS + 15.0
"""Deadline the app lifespan puts on waiting for the cancelled indexer task.

The drain deadline plus room for the lock release. The lifespan waits with
``asyncio.wait`` (which, unlike ``wait_for``, does not re-cancel and then
await the task on timeout -- the drain defers repeated cancellations, so that
would wait the whole drain out again). Past the deadline the task is left
pending and logged; it dies with the process, and everything it could still
touch is either process-local or a lock the server drops with the connection.

What this bounds is the **lifespan's wait**, not the termination of the worker
threads themselves: a store call blocked on a peer that acknowledges packets
but never answers is not ended by ``statement_timeout`` (the server never runs
it) nor by keepalives (the peer is not dead). Those threads are daemons or
pool workers and go when the process does.
"""

MAX_ARTIFACTS_PER_REPOSITORY = 10_000
"""Bound on one repository's enumerated artifacts.

A registry is somebody else's system: this is what stops a runaway or hostile
one from turning a sweep into an unbounded loop. The bound is per repository
and an over-bound repository is treated exactly like one whose listing failed
-- skipped, ``sources_failed`` counted, removal disabled for the source --
while every *other* repository still reconciles. Deliberately not a
per-source prefix: a prefix cut at the same sorted position every sweep
would permanently starve the artifacts behind it (round-2 codex finding).
"""

MAX_NEW_FETCHES_PER_SWEEP = 1_000
"""Fetch budget: how many *new* artifacts one sweep will read per source.

Distinct from the enumeration bound above, and the reason a big source makes
progress instead of starving its tail: enumeration stays complete (so removal
and tag reconciliation stay correct for every artifact), while fetching --
the expensive part, one manifest plus up to three blobs each -- is capped.
Digests already in the catalog cost nothing against the budget, so each sweep
fetches the next batch beyond what earlier sweeps indexed. Artifacts past the
budget are counted as ``deferred`` and picked up next sweep.
"""

MAX_CARD_BYTES = 8 * 1024 * 1024
"""Cap on one card's JSON text. Bundle files are capped at 256 KiB each and a
card embeds at most a few of them; a card past this is not a Cog description,
it is a payload, and it is recorded as ``failed`` rather than stored."""

MAX_ERROR_CHARS = 500
"""Cap on one ``read_errors`` entry; the row is a diagnosis, not a dump."""

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
OUTCOME_DEFERRED = "deferred"

RETRY_SLOT_EVERY = 4
"""Every fourth fetch slot goes to a retry of a ``failed`` row (see :meth:`CogIndexer._schedule`)."""


@dataclass
class SweepSummary:
    """What one sweep did. Logged at the end and exported as counters."""

    indexed: int = 0
    skipped: int = 0
    retagged: int = 0
    non_cog: int = 0
    failed: int = 0
    removed: int = 0
    deferred: int = 0
    """New artifacts past this sweep's fetch budget; next sweep's work."""
    sources: int = 0
    sources_failed: int = 0
    """Sources whose enumeration did not complete; their removal step was skipped."""
    locked_out: bool = False
    """Another replica held the sweep lock; nothing was done."""
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    """Source-level (not per-artifact) failures: stage + exception class, never URLs."""

    def as_log_fields(self) -> dict[str, object]:
        fields = asdict(self)
        fields["errors"] = len(self.errors)
        return fields


@dataclass
class _Enumeration:
    """One source's artifacts as seen this sweep, and whether the picture is complete."""

    present: dict[str, list[ArtifactRef]] = field(default_factory=dict)
    complete: bool = True
    """Whether the *repository list* itself was obtained. False = nothing can be declared gone."""
    failed_repositories: list[str] = field(default_factory=list)
    """Repositories whose own listing failed or was refused: reconciled nothing, removal skipped for them only."""
    errors: list[str] = field(default_factory=list)


class CogIndexer:
    """The reconciliation loop over a set of registry sources and one catalog store."""

    def __init__(
        self,
        store: CogCatalogStore,
        sources: Sequence[RegistrySource],
        *,
        max_artifacts_per_repository: int = MAX_ARTIFACTS_PER_REPOSITORY,
        max_new_fetches_per_sweep: int = MAX_NEW_FETCHES_PER_SWEEP,
        max_bytes_per_file: int = DEFAULT_MAX_BUNDLE_FILE_BYTES,
        drain_deadline_seconds: float = DRAIN_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._sources = list(sources)
        self._max_artifacts = max_artifacts_per_repository
        self._drain_deadline = drain_deadline_seconds
        self._max_new_fetches = max_new_fetches_per_sweep
        self._max_bytes_per_file = max_bytes_per_file
        self._clock = clock
        self.last_summary: SweepSummary | None = None
        self._executor = ThreadPoolExecutor(max_workers=STORE_WORKER_THREADS, thread_name_prefix="cog-index-store")
        # Workers a cancelled sweep stopped waiting for (see _drain): they
        # still own whatever they were doing, so the lock release that would
        # have followed them is handed to a thread instead.
        self._abandoned: list[Future] = []
        self._late_releases: set[threading.Thread] = set()
        # Per-source fairness state for the fetch budget (see _schedule): the
        # interleave phase advances every sweep, and the retry order is a
        # rotating list of (repository, digest) -- identities, not positions,
        # so candidates entering or leaving between sweeps cannot make the
        # rotation skip one.
        self._phase: dict[str, int] = {}
        self._retry_order: dict[str, list[tuple[str, str]]] = {}

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources)

    @property
    def pending_late_releases(self) -> int:
        """Lock releases handed off to threads that have not completed yet."""

        self._late_releases = {thread for thread in self._late_releases if thread.is_alive()}
        return len(self._late_releases)

    def close(self) -> None:
        """Stop accepting store calls. Does not interrupt calls already running.

        ``cancel_futures`` drops work that never started; a submitted call that
        is already on a thread runs to completion, which is what the lock
        hand-off waits for.
        """

        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- threading discipline ---------------------------------------------------

    async def _on_thread(self, func, /, *args, **kwargs):
        """Run a blocking store call on a worker thread; cancellation drains it, bounded.

        A worker thread cannot be interrupted, so a plain ``await
        asyncio.to_thread(...)`` cancelled mid-call leaves the call running
        after the coroutine has moved on -- which for this module means a
        sweep could release its lock while a write is still mutating rows, or
        abandon a lock acquisition that completes a moment later with nobody
        left to release it. So the future is shielded, and on cancellation
        this waits for the thread to finish (its result or error discarded)
        before propagating.

        The drain itself is shielded and deadline-bounded (see
        :meth:`_drain`): repeated cancellations are deferred until the worker
        completes -- they collapse into the one ``CancelledError`` re-raised
        here. A worker still running at the deadline (a dead connection the
        server cannot abort) is recorded in ``_abandoned``: the coroutine
        moves on, but :meth:`_release` will not release the lock until that
        worker has finished -- it hands the release to a thread.

        The call is submitted to this indexer's own executor rather than
        ``asyncio.to_thread``, so the ``concurrent.futures.Future`` is an
        object this module owns: the hand-off thread can wait on it without
        an event loop.

        The worker's outcome travels as a **value**, never as the future's
        exception, and is re-raised here. That is not style: ``asyncio.shield``
        attaches a logger to the inner future when the outer is cancelled
        (``asyncio/tasks.py``'s ``_log_on_exception``), and it hands the raw
        exception to the loop's exception handler -- for a store call, an
        exception whose text can name the database URL this module is careful
        never to log. A future that never fails has nothing to report.
        """

        worker = self._executor.submit(_capture, func, args, kwargs)
        future = asyncio.wrap_future(worker)
        try:
            failed, value = await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._drain(future, worker)
            raise
        if failed:
            raise value
        return value

    def _observe(self, worker: Future) -> None:
        """Log an abandoned worker's failure, by class name, when it eventually lands.

        An abandoned future is dropped by whoever handed it off, so without
        this its outcome would go unrecorded -- and a store call that failed
        after the sweep gave up on it is worth a line.
        """

        def consume(done: Future) -> None:
            if done.cancelled():
                return
            outcome = done.result()
            failed, value = outcome
            if failed:
                logger.error("cog_index_abandoned_worker_failed", extra={"error": type(value).__name__})

        worker.add_done_callback(consume)

    async def _drain(self, future: asyncio.Future, worker: Future) -> None:
        """Wait for a cancelled call's worker, deferring further cancels, up to the deadline."""

        deadline = self._clock() + self._drain_deadline
        while not future.done():
            remaining = deadline - self._clock()
            if remaining <= 0:
                # The thread is stuck on something no timeout reached (a dead
                # transport, most likely). Stop waiting -- hanging shutdown
                # forever helps nobody -- but do not pretend it finished: the
                # release that follows must wait for it (see _release), and
                # whoever ends up dropping it has already arranged for its
                # outcome to be consumed.
                logger.error(
                    "cog_index_worker_drain_expired",
                    extra={"deadline_seconds": self._drain_deadline},
                )
                self._observe(worker)
                self._abandoned.append(worker)
                return
            try:
                # Shielded: a second cancellation must interrupt this wait
                # without touching the worker, and then wait again -- the
                # whole point of the drain is that the lock is not released
                # while a write is still running, however often the task is
                # cancelled. The deferred cancellations collapse into the one
                # CancelledError the caller re-raises.
                await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
            except asyncio.CancelledError:
                continue
            except TimeoutError:
                continue
            except Exception:
                break  # the worker's own failure: it is done, which is all the drain wants
        if future.done() and not future.cancelled():
            # Consume the result/exception so nothing warns about it later.
            with suppress(Exception):
                future.exception()

    async def _release(self, lock) -> None:
        """Release the sweep lock, tolerating both errors and cancellation.

        Runs in ``finally`` blocks, where raising would mask whatever ended
        the sweep; an exit failure is logged by class name only. Cancellation
        during the release drains like every other store call, so the unlock
        (and the pooled connection's return) always completes.

        If an earlier store call of this sweep is still running (its drain
        expired), the lock is **not** released here: a release under a live
        worker is exactly the state single-flight exists to prevent -- another
        replica would acquire and a late write would land under its lock.
        The release is handed to :meth:`_hand_off` instead, which waits for
        the worker without a deadline and then exits the lock.
        """

        if self._abandoned:
            pending, self._abandoned = self._abandoned, []
            self._hand_off(lock, pending)
            return
        try:
            await self._on_thread(lock.__exit__, None, None, None)
        except asyncio.CancelledError:
            if self._abandoned:
                # The unlock itself outlived the drain deadline. It is still
                # running on its thread and will finish the exit on its own,
                # so there is nothing to hand off -- but the lock object must
                # stay reachable until it does, or finalizing the context
                # manager would run the very unlock we are waiting for on
                # whatever thread happens to collect it.
                self._hand_off(None, self._abandoned)
                self._abandoned = []
                logger.error("cog_index_lock_release_outlived_drain")
            raise
        except Exception as exc:
            logger.error("cog_index_lock_release_failed", extra={"error": type(exc).__name__})

    def _hand_off(self, lock, workers: list[Future]) -> None:
        """Wait out abandoned workers on a daemon thread -- no deadline -- then exit ``lock``.

        A thread rather than a task because this has to outlive the event
        loop. At shutdown every task is cancelled and the loop closes; an
        asyncio owner would stop mid-wait, drop the last reference to the lock
        context, and leave the garbage collector to run its ``finally`` --
        unlocking while the write was still in flight, which is the one thing
        the drain exists to prevent.

        ``lock`` is ``None`` when the unlock is itself the abandoned call: the
        thread then only holds the reference and waits, so nothing finalizes
        the context manager underneath it.

        The thread itself is a daemon, but that does **not** mean the process
        can always leave: the blocked call is running on a
        ``ThreadPoolExecutor`` worker, and those are joined at interpreter
        exit. A worker blocked forever therefore needs the process killed from
        outside. What bounds the wait in every lesser case is the worker:
        ``statement_timeout`` where the transport works, keepalives where the
        peer is gone.

        **Known gap (issue #128).** If the process does die here, the
        lock and the in-flight write are on *different* pooled connections, so
        the server can drop the lock session first and let another replica in
        while the old write is still landing. Nothing in this module orders
        those two sessions; closing it needs the sweep's writes to run on the
        session that holds the lock, or a fencing token they carry.
        """

        def run() -> None:
            for worker in workers:
                with suppress(BaseException):
                    worker.result()
            if lock is None:
                return
            try:
                lock.__exit__(None, None, None)
            except Exception as exc:
                logger.error("cog_index_lock_release_failed", extra={"error": type(exc).__name__, "late": True})
                return
            logger.warning("cog_index_lock_released_late", extra={"workers": len(workers)})

        thread = threading.Thread(target=run, name="cog-index-late-release", daemon=True)
        self._late_releases.add(thread)
        thread.start()
        logger.error("cog_index_lock_release_deferred", extra={"workers": len(workers)})

    # -- the sweep ------------------------------------------------------------

    async def sweep(self) -> SweepSummary:
        """Reconcile every source once, under the sweep lock.

        Never raises for a per-artifact or per-source failure; does raise for
        a database outage (the store's own errors) and propagates cancellation
        -- in both cases after the lock is released.
        """

        started = self._clock()
        summary = SweepSummary(sources=len(self._sources))
        # Workers a targeted reindex (which takes no lock) may have left
        # behind are not this sweep's to wait for. Dropping the reference is
        # safe: nothing is waiting on their result, they hold no lock, and
        # _observe already arranged for their outcome to be consumed.
        self._abandoned = []
        # The lock is taken and released on a worker thread: it is a blocking
        # database call, and the connection it occupies stays checked out for
        # the whole sweep (see the store's sweep_lock contract).
        lock = self._store.sweep_lock()
        try:
            try:
                acquired = await self._on_thread(lock.__enter__)
            except asyncio.CancelledError:
                # _on_thread drained the worker, so __enter__ finished on its
                # thread even though this coroutine was cancelled. Whatever it
                # entered gets its matching exit before the cancellation
                # propagates; if __enter__ itself failed there is nothing
                # held and _release swallows the mismatched exit.
                await self._release(lock)
                raise
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
                await self._release(lock)
        finally:
            summary.duration_seconds = self._clock() - started
            COG_INDEX_SWEEP_DURATION.observe(summary.duration_seconds)
            self.last_summary = summary
            logger.info("cog_index_sweep", extra=summary.as_log_fields())
        return summary

    async def _sweep_source(self, source: RegistrySource, summary: SweepSummary) -> None:
        enumeration = await self._enumerate(source)
        if enumeration.errors:
            summary.sources_failed += 1
            for error in enumeration.errors:
                summary.errors.append(f"{source.id}: {error}")
                logger.warning("cog_index_source_failed", extra={"source": source.id, "reason": error})
        if not enumeration.complete:
            # No repository list: nothing to reconcile and nothing that can be
            # declared gone.
            return

        known = {(row.repository, row.digest): row for row in await self._on_thread(self._store.known, source.id)}
        # Three classes, mirroring _reconcile's decision: bookkeeping (known,
        # not failed) costs no budget; a fetch happens for digests the catalog
        # has never seen and for retries of failed rows. Which fetches get
        # this sweep's budget is decided by _schedule, which interleaves the
        # two classes so neither can starve the other.
        unseen: list[tuple[str, ArtifactRef, None]] = []
        retries: list[tuple[str, ArtifactRef, KnownArtifact]] = []
        for repository, artifacts in enumeration.present.items():
            for artifact in artifacts:
                known_row = known.get((repository, artifact.digest))
                if known_row is None:
                    unseen.append((repository, artifact, None))
                elif known_row.status == STATUS_FAILED:
                    retries.append((repository, artifact, known_row))
                else:
                    outcome = await self._reconcile(source, repository, artifact, known_row)
                    _count(summary, outcome)
        fetch, deferred = self._schedule(source.id, unseen, retries)
        for repository, artifact, known_row in fetch:
            if known_row is not None:
                # Recorded as the attempt begins, not when the sweep was
                # planned: a sweep that raises or is cancelled partway must
                # not rotate past candidates it never tried.
                self._note_retry_attempt(source.id, (repository, artifact.digest))
            _count(summary, await self._reconcile(source, repository, artifact, known_row))
        for _ in range(deferred):
            _count(summary, OUTCOME_DEFERRED)

        # Removal is per repository: what was not enumerated cannot be
        # declared gone, so repositories whose listing failed keep their rows
        # untouched, while every fully listed repository -- and every
        # repository that has vanished from the source's list -- is
        # reconciled. Deferred fetches do NOT skip removal: those artifacts
        # were enumerated and stand in the present set.
        present = {repo: [artifact.digest for artifact in artifacts] for repo, artifacts in enumeration.present.items()}
        removed = await self._on_thread(
            self._store.mark_removed, source.id, present, excluding=enumeration.failed_repositories
        )
        summary.removed += removed
        if removed:
            COG_INDEX_ARTIFACTS.labels(outcome=OUTCOME_REMOVED).inc(removed)

    def _schedule(self, source_id: str, unseen: list, retries: list) -> tuple[list, int]:
        """Split this sweep's fetches into (fetch now, number deferred) so neither class starves.

        Slots are dealt in a fixed pattern -- every ``RETRY_SLOT_EVERY``-th
        slot to a retry of a failed row, the rest to never-seen artifacts --
        with a class that has run out yielding its slot to the other. Two
        pieces of per-source state make it fair *across* sweeps, not just
        within one: the pattern's **phase** advances each sweep, so even a
        budget of one alternates classes over successive sweeps instead of
        always serving slot zero; and retries are served from a **rotating
        order of identities** (see :meth:`_retry_candidates`), so a stable
        prefix of permanent failures cannot occupy the retry slots forever
        while a recoverable failure behind it waits. Never-seen artifacts keep
        enumeration order (once indexed they cost nothing, so the tail is
        reached without rotation).
        """

        phase = self._phase.get(source_id, 0)
        self._phase[source_id] = (phase + 1) % RETRY_SLOT_EVERY
        unseen_queue = list(unseen)
        retry_queue = self._retry_candidates(source_id, retries)
        fetch: list = []
        slot = phase
        while len(fetch) < self._max_new_fetches and (unseen_queue or retry_queue):
            take_retry = slot % RETRY_SLOT_EVERY == RETRY_SLOT_EVERY - 1
            if (take_retry and retry_queue) or not unseen_queue:
                fetch.append(retry_queue.pop(0))
            else:
                fetch.append(unseen_queue.pop(0))
            slot += 1
        return fetch, len(unseen_queue) + len(retry_queue)

    def _retry_candidates(self, source_id: str, retries: list) -> list:
        """This source's failed rows in rotation order, oldest attempt first.

        The order is remembered as ``(repository, digest)`` identities rather
        than a position, because the candidate list changes between sweeps: a
        newly failed artifact can appear anywhere in enumeration order, and a
        recovered one disappears. A positional cursor moved by those edits and
        could step over a candidate every sweep, which is the starvation this
        is here to prevent. Remembered identities that are still failing keep
        their order; ones that are new go behind them; ones that are gone are
        forgotten, so the state stays the size of the failed set.
        """

        by_key = {
            (repository, artifact.digest): (repository, artifact, known) for repository, artifact, known in retries
        }
        remembered = [key for key in self._retry_order.get(source_id, ()) if key in by_key]
        seen = set(remembered)
        order = remembered + [key for key in by_key if key not in seen]
        self._retry_order[source_id] = order
        return [by_key[key] for key in order]

    def _note_retry_attempt(self, source_id: str, key: tuple[str, str]) -> None:
        """Move one identity to the back of its source's rotation, as its attempt begins."""

        order = self._retry_order.get(source_id)
        if order and key in order:
            order.remove(key)
            order.append(key)

    async def _enumerate(self, source: RegistrySource) -> _Enumeration:
        # Broad excepts on purpose: adapters talk to third-party systems and
        # this seam is the isolation boundary -- an unexpected exception from
        # one source (a client bug, a malformed response nobody anticipated)
        # must cost that source's sweep, not the other sources'. Store errors
        # never pass through here, so a database outage still aborts.
        result = _Enumeration()
        try:
            repositories = await source.list_repositories()
        except Exception as exc:
            result.complete = False
            result.errors.append(f"list_repositories: {_describe(exc)}")
            return result
        for repository in repositories:
            try:
                artifacts = await source.list_artifacts(repository)
            except OCINotFound:
                # A configured repository that does not exist (yet) has no
                # artifacts; that is an answer, not a failure, and its rows
                # -- if it ever had any -- are correctly marked removed.
                artifacts = []
            except Exception as exc:
                result.failed_repositories.append(repository)
                result.errors.append(f"list_artifacts {repository}: {_describe(exc)}; removal skipped for it")
                continue
            if len(artifacts) > self._max_artifacts:
                # Treated exactly like a failed listing of this one
                # repository, and never as a prefix: enumeration always runs
                # to the end of the repository list, so the artifacts behind
                # an over-bound repository are not starved and the bound
                # cannot silently misrepresent a partial view as complete.
                # Only this repository's rows are shielded from removal.
                logger.warning(
                    "cog_index_repository_over_bound",
                    extra={"source": source.id, "repository": repository, "bound": self._max_artifacts},
                )
                result.failed_repositories.append(repository)
                result.errors.append(
                    f"list_artifacts {repository}: over the {self._max_artifacts}-artifact bound;"
                    " removal skipped for it"
                )
                continue
            result.present[repository] = artifacts
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
            await self._on_thread(
                self._store.update_tags, source.id, repository, artifact.digest, tags, pushed_at=artifact.pushed_at
            )
            return OUTCOME_RETAGGED
        row = await self._read_artifact(source, repository, artifact)
        row = await self._store_row(row)
        if row.status == STATUS_INDEXED:
            return OUTCOME_INDEXED
        if row.status == STATUS_NON_COG:
            return OUTCOME_NON_COG
        return OUTCOME_FAILED

    async def _store_row(self, row: CogArtifact) -> CogArtifact:
        """Upsert the row; a card the database refuses becomes a ``failed`` row, not a poisoned sweep.

        The card is pre-validated (:func:`_card_unstorable`), so this catch is
        the second line: whatever representability rule the database enforces
        that the validation did not anticipate. Only the store's *data* error
        is caught -- an outage raises through, because retrying every artifact
        against a dead database is not resilience.
        """

        try:
            await self._on_thread(self._store.upsert, row)
            return row
        except CogCatalogDataError as exc:
            fallback = _with(row, status=STATUS_FAILED, card=None, read_errors=_errors(f"store: {type(exc).__name__}"))
            await self._on_thread(self._store.upsert, fallback)
            return fallback

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
        return await self._store_row(row)

    async def mark_removed(self, source_id: str, repository: str, digest: str) -> bool:
        """Mark one artifact removed (a delete event). Returns whether a present row was marked."""

        self._source(source_id)
        marked = await self._on_thread(self._store.mark_removed_one, source_id, repository, digest)
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
        """Turn one enumerated artifact into the row to store. Never raises except for cancellation.

        The guard is deliberately broader than the OCI hierarchy: the client's
        contract is to wrap its failures, but a contract is not a proof, and a
        failure it missed (a header-encoding bug, an unanticipated response
        shape) must cost this one row -- recorded by class name only, since an
        unknown exception's message is not known to be safe -- never the sweep.
        ``except Exception`` does not catch ``CancelledError``, so cancellation
        still propagates.
        """

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
            if _bundle_layer(manifest, COG_ENTRY_FILE) is None:
                return await self._read_without_entry(client, repository, manifest, base)
            files = await self._fetch_cog_files(client, repository, manifest)
            card = read_cog_bundle(files, bundle_paths=_titles(manifest))
            return _indexed(base, card)
        except OCIError as exc:
            # Recorded, not raised: one broken artifact must not stop a sweep.
            # OCI messages are kept -- the client's contract is that they name
            # neither URLs nor headers.
            return _with(base, status=STATUS_FAILED, read_errors=_errors(f"fetch: {_describe(exc)}"))
        except Exception as exc:
            return _with(base, status=STATUS_FAILED, read_errors=_errors(f"read: {type(exc).__name__}"))

    async def _fetch_cog_files(self, client, repository: str, manifest: Manifest) -> dict[str, bytes]:
        """``COG.md`` first, then the profile file its frontmatter names (plus ``pixi.toml`` for tasks).

        The manifest pointer is only known after ``COG.md`` is parsed, so this
        is two rounds of fetching rather than one guess at a file name. Never
        the lockfile: ``select_bundle_layers`` drops it by title *and* media
        type, and the direct entry fetch applies the same media-type exclusion
        (:func:`_bundle_layer`) so a layer titled ``COG.md`` that is really
        the lockfile is not fetched either.
        """

        entry_layer = _bundle_layer(manifest, COG_ENTRY_FILE)
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

        pixi = _bundle_layer(manifest, PIXI_MANIFEST)
        if pixi is None:
            return _with(
                base,
                status=STATUS_NON_COG,
                read_errors=_errors(
                    f"manifest carries no {COG_ENTRY_FILE} or {PIXI_MANIFEST} layer; {_layer_summary(manifest)}"
                ),
            )
        files = {PIXI_MANIFEST: await client.get_blob(repository, pixi, max_bytes=self._max_bytes_per_file)}
        card = read_cog_bundle(files, bundle_paths=_titles(manifest))
        if card.kind == PROG_KIND and card.profile_status == PROFILE_PARSED:
            return _indexed(base, card)
        return _with(
            base,
            status=STATUS_NON_COG,
            read_errors=_errors(*card.errors) or _errors(f"manifest carries no {COG_ENTRY_FILE} layer"),
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

        A sweep that raises (a store outage, a bug) is logged -- by exception
        class, never with a chain that could echo somebody's URL -- and the
        loop continues: the next interval retries. Cancellation propagates.
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
                except Exception as exc:
                    logger.error("cog_index_sweep_failed", extra={"error": type(exc).__name__})
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


def _capture(func, args, kwargs):
    """Run ``func`` on the worker thread and return ``(failed, value_or_error)``.

    See :meth:`CogIndexer._on_thread`: the outcome must not become the
    future's exception, or asyncio reports it for us with its text intact.
    """

    try:
        return False, func(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - re-raised by the caller, or logged by class name
        return True, exc


def _count(summary: SweepSummary, outcome: str) -> None:
    setattr(summary, outcome, getattr(summary, outcome) + 1)
    COG_INDEX_ARTIFACTS.labels(outcome=outcome).inc()


def _with(artifact: CogArtifact, **changes) -> CogArtifact:
    return replace(artifact, **changes)


def _errors(*items: str) -> tuple[str, ...]:
    """Bound and sanitize ``read_errors`` entries.

    Reader errors can embed published content (a conflicting name, a layer
    title), which may carry NUL -- the one character ``jsonb`` refuses -- and
    can be arbitrarily long. Each entry is NUL-escaped and capped so the error
    column can always be stored, whatever the card contained.
    """

    cleaned = []
    for item in items:
        item = item.replace("\x00", "\\x00")
        if len(item) > MAX_ERROR_CHARS:
            item = item[: MAX_ERROR_CHARS - 1] + "…"
        cleaned.append(item)
    return tuple(cleaned)


def _card_unstorable(document: dict) -> str | None:
    """Why this card cannot go into ``jsonb``, or ``None`` when it can.

    Postgres ``jsonb`` refuses ``\\u0000`` anywhere in a string, and the
    catalog refuses to be a blob store. Checked *before* the insert so one
    unstorable card is one ``failed`` row instead of a database error that
    poisons every later sweep.
    """

    if contains_nul(document):
        # Checked on the PARSED values, never by searching the serialized
        # text: json.dumps escapes a literal backslash, so the substring
        # "\\u0000" also appears for the harmless literal backslash-u-0-0-0-0
        # in a doc -- a card discussing NUL must index; only a card
        # *containing* one may not (round-2 codex finding).
        return "card: contains NUL (\\u0000), which jsonb cannot store"
    try:
        dumped = json.dumps(document)
    except (TypeError, ValueError) as exc:
        return f"card: not JSON-serializable ({type(exc).__name__})"
    if len(dumped) > MAX_CARD_BYTES:
        return f"card: {len(dumped)} bytes of JSON exceeds the {MAX_CARD_BYTES}-byte cap"
    return None


def _indexed(base: CogArtifact, card: CogCard) -> CogArtifact:
    document = card.to_dict()
    issue = _card_unstorable(document)
    if issue is not None:
        return _with(base, status=STATUS_FAILED, card=None, read_errors=_errors(issue, *card.errors))
    return _with(
        base,
        status=STATUS_INDEXED,
        card=document,
        read_errors=_errors(*card.errors),
        **card_search_fields(document),
    )


def _bundle_layer(manifest: Manifest, title: str) -> Descriptor | None:
    """The layer carrying ``title``, unless it is really the lockfile.

    The same media-type exclusion ``select_bundle_layers`` applies, enforced
    on the direct fetches too: a descriptor *titled* ``COG.md`` or
    ``pixi.toml`` but carrying the lockfile media type is the one large layer
    the card never needs, whatever its title claims.
    """

    layer = manifest.layer_by_title(title)
    if layer is None or layer.media_type == MEDIA_TYPE_PIXI_LOCK:
        return None
    return layer


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
    """One failure as ``read_errors``/log text: class name, plus the message only when it is known safe.

    The OCI hierarchy's contract is that messages carry neither URLs nor
    headers, so those messages are diagnostic and kept. Anything else --
    adapter errors whose text may quote a configured URL, unexpected
    exceptions whose text may quote anything -- is recorded by class alone.
    """

    if isinstance(exc, OCIError):
        message = str(exc)
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return type(exc).__name__
