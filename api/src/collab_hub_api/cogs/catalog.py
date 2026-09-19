"""The Cog catalog: where indexed artifacts live (issue #84).

One row per ``(source_id, repository, digest)`` in ``collab_cog_artifacts``,
created by migration version 11 of :mod:`..frames.collab_schema` -- never by
this module, which carries no DDL. Identity is the digest: ``cog_id`` and
``name`` are search keys read from the Cog's own declarations, and the
repository path is not identity (published repository names carry an id
suffix, and one Cog may be published to several repositories).

Three backends, following the house pattern of the other relational stores:

- :class:`PostgresCogCatalogStore` over the shared ``frames.postgres`` pool;
- :class:`InMemoryCogCatalogStore` for tests and single-process development;
- :class:`UnavailableCogCatalogStore` when no database is configured, which
  raises :class:`CogCatalogUnavailableError` (-> 503 at the API) rather than
  answering an empty catalog as if it were the truth.

**Content policy.** ``card`` is the published bundle's own declarations,
stored verbatim (parent-issue acceptance: the reader's output, structure
preserved) -- so whatever a publisher writes there is what the catalog holds.
The guarantee is narrower and absolute: *configured registry credentials*
never reach cards, ``read_errors``, or logs. The store additionally refuses
content ``jsonb`` cannot represent (:class:`CogCatalogDataError`), so one
pathological card can be recorded as failed instead of poisoning writes.

The write surface is exactly what the reconciliation indexer needs
(:meth:`~CogCatalogStore.known`, :meth:`~CogCatalogStore.upsert`,
:meth:`~CogCatalogStore.update_tags`, :meth:`~CogCatalogStore.mark_removed`);
the read surface is what the catalog API (#85) builds on. Rows are never
deleted: installs and runs may reference a digest long after its publisher
removed it, so removal is ``removed_at = now()`` and the row stays readable.

Every method is synchronous psycopg over the shared pool, like every other
store here. The indexer is async and bridges with ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .registry import reference

STATUS_INDEXED = "indexed"
"""The reader produced a card. Errors on the card do not change this: a draft
or a Cog with a broken profile is still a Cog, listed with what it declared."""

STATUS_NON_COG = "non_cog"
"""The manifest carries no ``COG.md`` (and no Prog ``pixi.toml``). Recorded,
with a reason, so the digest is skipped on later sweeps instead of re-read."""

STATUS_FAILED = "failed"
"""Fetching or reading the bundle failed. ``read_errors`` says how; the next
sweep retries it."""

STATUSES = frozenset({STATUS_INDEXED, STATUS_NON_COG, STATUS_FAILED})

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

# Session-level advisory-lock key the indexer's sweep takes so replicas do not
# sweep concurrently. Same `int.from_bytes(<8 ASCII bytes>, "big")` derivation
# as COLLAB_SCHEMA_LOCK_KEY ("collab_1") and FRAMES_SERVER_SCHEMA_LOCK_KEY
# ("fsvrddl1"), so the constant is greppable and distinct from both.
COG_INDEX_LOCK_KEY = int.from_bytes(b"cogidx_1", "big")

SWEEP_STATEMENT_TIMEOUT_SECONDS = 20.0
"""Server-side bound on every statement the sweep runs (lock included).

The pool timeout bounds *checkout*; nothing else bounds execution, and a
sweep's cancelled worker threads are drained before the lock is released --
so a statement the server never finishes would otherwise stall shutdown for
the whole drain deadline. Set transaction-locally on the sweep-path methods
(the connection goes back to the pool unaltered) and session-set/reset on the
lock connection (which runs in autocommit). Deliberately below the indexer's
``DRAIN_DEADLINE_SECONDS`` so the server's abort fires first whenever the
transport still works; a dead transport is what the drain deadline is for.
"""


class CogCatalogUnavailableError(RuntimeError):
    """Raised when the catalog is needed but no backend is configured."""


class CogCatalogDataError(ValueError):
    """This one row's content cannot be stored (e.g. NUL in the card's JSON).

    Deliberately distinct from the store's availability/outage errors: the
    indexer records a row raising this as ``failed`` and continues the sweep,
    while an outage aborts the sweep. Both backends raise it for the same
    content so tests against the in-memory store see the production behavior.
    """


@dataclass(frozen=True)
class CogArtifact:
    """One row of the catalog: one artifact at one location in one source."""

    source_id: str
    host: str
    repository: str
    digest: str
    status: str
    tags: tuple[str, ...] = ()
    pushed_at: datetime | None = None
    indexed_at: datetime | None = None
    manifest_media_type: str | None = None
    card: dict[str, Any] | None = None
    cog_id: str | None = None
    name: str | None = None
    version: str | None = None
    kind: str | None = None
    publisher: str | None = None
    manifest_schema: str | None = None
    read_errors: tuple[str, ...] = ()
    removed_at: datetime | None = None

    @property
    def reference(self) -> str:
        """The pinned install reference ``<host>/<repository>@<digest>``."""

        return reference(self.host, self.repository, self.digest)

    @property
    def present(self) -> bool:
        return self.removed_at is None


logger = logging.getLogger("frames_server.cogs.catalog")


@dataclass(frozen=True)
class KnownArtifact:
    """What the indexer needs to know about a row before deciding to refetch it."""

    repository: str
    digest: str
    tags: tuple[str, ...]
    status: str
    removed: bool


@dataclass(frozen=True)
class CatalogFilter:
    """Filters for :meth:`CogCatalogStore.list_current`.

    ``provides``/``requires``/``accepts``/``produces`` are containment filters
    over the structured card (``card @> ...``), which is what the GIN index
    on ``card`` serves. ``requires`` names a capability
    (``card.requires[*].capability``); ``provides`` an entry of
    ``card.provides``; ``accepts``/``produces`` entries of ``card.io``.
    """

    kind: str | None = None
    publisher: str | None = None
    source_id: str | None = None
    provides: str | None = None
    requires: str | None = None
    accepts: str | None = None
    produces: str | None = None

    def containment(self) -> dict[str, Any] | None:
        """The JSON document the card must contain, or ``None`` when no card filter is set."""

        document: dict[str, Any] = {}
        if self.provides is not None:
            document["provides"] = [self.provides]
        if self.requires is not None:
            document["requires"] = [{"capability": self.requires}]
        io: dict[str, Any] = {}
        if self.accepts is not None:
            io["accepts"] = [self.accepts]
        if self.produces is not None:
            io["produces"] = [self.produces]
        if io:
            document["io"] = io
        return document or None


class CogCatalogStore(ABC):
    """The catalog relation, as the indexer writes it and the API reads it."""

    # -- what the indexer needs ---------------------------------------------

    @abstractmethod
    def known(self, source_id: str) -> list[KnownArtifact]:
        """Every row this source has ever produced, removed rows included."""

        raise NotImplementedError

    @abstractmethod
    def upsert(self, artifact: CogArtifact, *, targeted: bool = False) -> None:
        """Insert the row or replace it whole; a replaced row is present again (``removed_at`` cleared).

        ``targeted`` marks the webhook receiver's lock-less ``reindex`` path:
        a targeted upsert always checks out its own pooled connection instead
        of riding a running sweep's lock session (issue #128), so it works
        concurrently with a sweep. Sweep-path upserts leave it ``False``.
        Backends without sessions ignore the flag.
        """

        raise NotImplementedError

    @abstractmethod
    def update_tags(
        self,
        source_id: str,
        repository: str,
        digest: str,
        tags: Iterable[str],
        *,
        pushed_at: datetime | None = None,
    ) -> bool:
        """Change a row's tag set without touching its card, and mark it present again.

        Returns whether a row existed. ``pushed_at`` is updated only when given.
        """

        raise NotImplementedError

    @abstractmethod
    def mark_removed(
        self, source_id: str, present: Mapping[str, Iterable[str]], *, excluding: Iterable[str] = ()
    ) -> int:
        """Set ``removed_at = now()`` on this source's present rows whose digest is not in ``present``.

        ``present`` maps repository -> digests enumerated this sweep; rows in
        a repository listed in ``excluding`` (one whose enumeration failed
        this sweep) are left untouched, because what was not enumerated
        cannot be declared gone. Returns the number of rows newly marked.
        Never deletes.
        """

        raise NotImplementedError

    @abstractmethod
    def mark_removed_one(self, source_id: str, repository: str, digest: str) -> bool:
        """Mark one row removed (a webhook delete). Returns whether a present row was marked."""

        raise NotImplementedError

    @abstractmethod
    def sweep_lock(self) -> AbstractContextManager[bool]:
        """Try to become the one sweeper; yields whether the lock was taken.

        Non-blocking. Held for the whole sweep and released on exit whether or
        not the sweep succeeded. The Postgres store takes a **session-level**
        advisory lock on one pooled connection held outside any transaction
        (the sweep does network I/O for minutes; an open transaction that long
        would pin vacuum); a dead sweeper's lock is released by the server
        when its connection drops -- and its in-flight writes drop with it,
        because the sweep-path reads and writes run on that same connection
        while the lock is held (issue #128).
        """

        raise NotImplementedError

    # -- what the API needs ---------------------------------------------------

    @abstractmethod
    def get(self, digest: str, *, source_id: str | None = None, repository: str | None = None) -> CogArtifact | None:
        """One row by digest, removed rows included (an install may reference one).

        A digest can sit in several locations; without ``source_id``/``repository``
        the present row indexed most recently wins.
        """

        raise NotImplementedError

    @abstractmethod
    def locations(self, digest: str) -> list[CogArtifact]:
        """Every row carrying this digest, across sources and repositories."""

        raise NotImplementedError

    @abstractmethod
    def list_current(
        self,
        filters: CatalogFilter | None = None,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[CogArtifact]:
        """The newest present, indexed row per ``cog_id``, filtered.

        "Newest" is by ``pushed_at`` (unknown last), then ``indexed_at``.
        Non-Cog and failed rows, rows without a ``cog_id``, and removed rows
        never appear here.
        """

        raise NotImplementedError

    @abstractmethod
    def list_versions(self, cog_id: str, *, include_removed: bool = False) -> list[CogArtifact]:
        """Every indexed row for one ``cog_id``, newest first."""

        raise NotImplementedError


def _bounded_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_LIST_LIMIT)


def _sort_key(artifact: CogArtifact) -> tuple:
    # pushed_at DESC NULLS LAST, indexed_at DESC, then a stable tiebreak.
    floor = datetime.min.replace(tzinfo=UTC)
    return (
        artifact.pushed_at is None,
        -(artifact.pushed_at or floor).timestamp(),
        -(artifact.indexed_at or floor).timestamp(),
        artifact.source_id,
        artifact.repository,
        artifact.digest,
    )


def _get_key(artifact: CogArtifact) -> tuple:
    # get()'s documented order: present rows first, then the most recently
    # INDEXED location -- pushed_at deliberately plays no part here, because
    # every location of one digest shares the artifact's push time while
    # indexed_at says which row this catalog wrote about it last.
    floor = datetime.min.replace(tzinfo=UTC)
    return (
        artifact.removed_at is not None,
        -(artifact.indexed_at or floor).timestamp(),
        artifact.source_id,
        artifact.repository,
    )


def require_aware(value: datetime | None, what: str) -> datetime | None:
    """Refuse a naive datetime at the store boundary.

    ``timestamptz`` interprets a naive value in the session's timezone and the
    in-memory store would compare it against aware ones (a ``TypeError`` at
    sort time, far from the caller) -- both are the wrong place to discover
    the mistake. Adapters already normalize to aware UTC; this guards direct
    store and ``reindex`` callers.
    """

    if value is not None and value.utcoffset() is None:
        # utcoffset() covers both shapes of naivety: tzinfo absent, and a
        # tzinfo present whose utcoffset() returns None (datetime.tzinfo
        # allows that, and such a value is naive in every way that matters).
        raise ValueError(f"{what} must be timezone-aware; a naive datetime would be reinterpreted per session")
    return value


def contains_nul(value: Any) -> bool:
    """Whether any string in this JSON tree contains an actual NUL character.

    Walks the PARSED values (keys included). Never implemented by searching
    the serialized text: ``json.dumps`` escapes a literal backslash, so the
    substring ``\\u0000`` appears both for a real NUL and for the harmless
    six characters backslash-u-0-0-0-0 in a document about NUL -- and only the
    first is something ``jsonb`` refuses.
    """

    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_nul(item) for item in value)
    return False


def _scalar_eq(a: Any, b: Any) -> bool:
    # jsonb: true/false and numbers are different types (1 does not contain
    # true), while Python's `1 == True`. Numbers compare numerically across
    # int/float, matching jsonb's numeric comparison.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def json_contains(document: Any, needle: Any, *, _top: bool = True) -> bool:
    """Postgres ``jsonb @>`` containment, mirrored exactly for the in-memory store.

    Objects: every key of ``needle`` is in ``document`` with a contained value.
    Arrays: every element of ``needle`` is contained by *some* element of
    ``document``; an array needle never matches a scalar document. Scalars:
    equality, with booleans distinct from numbers. The one asymmetry jsonb
    grants -- an array contains a bare scalar -- applies at the **top level
    only**, exactly as documented for ``@>``.

    Pinned against a live server by a shared case table in
    ``test_cog_catalog.py``, so a divergence fails a test rather than making
    a filter answer differently in dev and production.
    """

    if isinstance(needle, dict):
        return isinstance(document, dict) and all(
            k in document and json_contains(document[k], v, _top=False) for k, v in needle.items()
        )
    if isinstance(needle, list):
        if not isinstance(document, list):
            return False
        return all(any(json_contains(item, wanted, _top=False) for item in document) for wanted in needle)
    if isinstance(document, list):
        return _top and any(_scalar_eq(item, needle) for item in document if not isinstance(item, (dict, list)))
    if isinstance(document, dict):
        return False
    return _scalar_eq(document, needle)


class UnavailableCogCatalogStore(CogCatalogStore):
    """Store used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> CogCatalogUnavailableError:
        return CogCatalogUnavailableError("Cog catalog storage is not configured")

    def known(self, source_id: str) -> list[KnownArtifact]:
        raise self._refuse()

    def upsert(self, artifact: CogArtifact, *, targeted: bool = False) -> None:
        raise self._refuse()

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        raise self._refuse()

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
        raise self._refuse()

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        raise self._refuse()

    def sweep_lock(self) -> AbstractContextManager[bool]:
        raise self._refuse()

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        raise self._refuse()

    def locations(self, digest) -> list[CogArtifact]:
        raise self._refuse()

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT) -> list[CogArtifact]:
        raise self._refuse()

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        raise self._refuse()


@dataclass
class InMemoryCogCatalogStore(CogCatalogStore):
    """Process-local catalog for tests and single-process development.

    Same semantics as the Postgres store, including containment filtering
    (:func:`json_contains`) and a process-local, non-blocking sweep lock.
    """

    _rows: dict[tuple[str, str, str], CogArtifact] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sweep: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, artifact: CogArtifact) -> tuple[str, str, str]:
        return (artifact.source_id, artifact.repository, artifact.digest)

    def known(self, source_id: str) -> list[KnownArtifact]:
        with self._lock:
            return [
                KnownArtifact(
                    repository=row.repository,
                    digest=row.digest,
                    tags=row.tags,
                    status=row.status,
                    removed=row.removed_at is not None,
                )
                for row in self._rows.values()
                if row.source_id == source_id
            ]

    def upsert(self, artifact: CogArtifact, *, targeted: bool = False) -> None:
        # ``targeted`` is about connection routing and this store has no
        # connections; accepted for signature parity with the Postgres store.
        if artifact.status not in STATUSES:
            raise ValueError(f"unknown catalog status {artifact.status!r}")
        require_aware(artifact.pushed_at, "pushed_at")
        require_aware(artifact.indexed_at, "indexed_at")
        if artifact.card is not None and contains_nul(artifact.card):
            # Parity with Postgres, where jsonb refuses NUL: a card this store
            # silently accepted would be a test passing on content production
            # rejects. contains_nul walks the parsed values, so a literal
            # backslash-u0000 in documentation text is (correctly) accepted.
            raise CogCatalogDataError("card contains NUL (\\u0000), which jsonb cannot store")
        stored = replace(
            artifact,
            tags=tuple(sorted(set(artifact.tags))),
            read_errors=tuple(artifact.read_errors),
            indexed_at=artifact.indexed_at or datetime.now(UTC),
            removed_at=None,
        )
        with self._lock:
            self._rows[self._key(stored)] = stored

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        require_aware(pushed_at, "pushed_at")
        with self._lock:
            row = self._rows.get((source_id, repository, digest))
            if row is None:
                return False
            self._rows[(source_id, repository, digest)] = replace(
                row,
                tags=tuple(sorted(set(tags))),
                pushed_at=pushed_at if pushed_at is not None else row.pushed_at,
                removed_at=None,
            )
            return True

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
        wanted = {repo: set(digests) for repo, digests in present.items()}
        shielded = set(excluding)
        now = datetime.now(UTC)
        marked = 0
        with self._lock:
            for key, row in list(self._rows.items()):
                if row.source_id != source_id or row.removed_at is not None:
                    continue
                if row.repository in shielded or row.digest in wanted.get(row.repository, ()):
                    continue
                self._rows[key] = replace(row, removed_at=now)
                marked += 1
        return marked

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        with self._lock:
            row = self._rows.get((source_id, repository, digest))
            if row is None or row.removed_at is not None:
                return False
            self._rows[(source_id, repository, digest)] = replace(row, removed_at=datetime.now(UTC))
            return True

    @contextmanager
    def sweep_lock(self):
        acquired = self._sweep.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._sweep.release()

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        candidates = [
            row
            for row in self.locations(digest)
            if (source_id is None or row.source_id == source_id)
            and (repository is None or row.repository == repository)
        ]
        if not candidates:
            return None
        candidates.sort(key=_get_key)
        return candidates[0]

    def locations(self, digest) -> list[CogArtifact]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.digest == digest]
        rows.sort(key=lambda row: (row.source_id, row.repository))
        return rows

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT) -> list[CogArtifact]:
        limit = _bounded_limit(limit)
        filters = filters or CatalogFilter()
        needle = filters.containment()
        with self._lock:
            rows = [
                row
                for row in self._rows.values()
                if row.removed_at is None
                and row.status == STATUS_INDEXED
                and row.cog_id is not None
                and (filters.kind is None or row.kind == filters.kind)
                and (filters.publisher is None or row.publisher == filters.publisher)
                and (filters.source_id is None or row.source_id == filters.source_id)
                and (needle is None or json_contains(row.card, needle))
            ]
        rows.sort(key=_sort_key)
        newest: dict[str, CogArtifact] = {}
        for row in rows:
            newest.setdefault(row.cog_id, row)  # type: ignore[arg-type]
        return sorted(newest.values(), key=lambda row: (row.cog_id or "", _sort_key(row)))[:limit]

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        with self._lock:
            rows = [
                row
                for row in self._rows.values()
                if row.cog_id == cog_id and row.status == STATUS_INDEXED and (include_removed or row.removed_at is None)
            ]
        rows.sort(key=_sort_key)
        return rows


_COLUMNS = (
    "source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type, status, card, "
    "cog_id, name, version, kind, publisher, manifest_schema, read_errors, removed_at"
)


def _row_to_artifact(row: Mapping[str, Any]) -> CogArtifact:
    card = row["card"]
    errors = row["read_errors"]
    return CogArtifact(
        source_id=row["source_id"],
        host=row["host"],
        repository=row["repository"],
        digest=row["digest"],
        status=row["status"],
        tags=tuple(row["tags"] or ()),
        pushed_at=row["pushed_at"],
        indexed_at=row["indexed_at"],
        manifest_media_type=row["manifest_media_type"],
        card=card if isinstance(card, dict) else None,
        cog_id=row["cog_id"],
        name=row["name"],
        version=row["version"],
        kind=row["kind"],
        publisher=row["publisher"],
        manifest_schema=row["manifest_schema"],
        read_errors=tuple(errors) if isinstance(errors, list) else (),
        removed_at=row["removed_at"],
    )


class PostgresCogCatalogStore(CogCatalogStore):
    """The catalog over ``collab_cog_artifacts`` on the shared pool.

    No ``_ensure_schema``: the table is created by the versioned, lock-guarded
    runner in :mod:`..frames.collab_schema` (version 11). A store that also
    emitted DDL would reintroduce the unlocked ``CREATE TABLE IF NOT EXISTS``
    race that runner exists to remove.
    """

    def __init__(self, db):
        self._db = db
        # The connection the sweep lock is currently held on, published by
        # _postgres_sweep_lock for exactly as long as the lock is acquired.
        # Sweep-path reads and writes run on it (see _sweep_connection), so a
        # crashed sweeper's writes die with its lock session (issue #128).
        # Written by the lock context and read by the sweep's store calls; no
        # guard, because the indexer sequences them: the acquire completes
        # before any sweep call is submitted, and every sweep call -- handed-
        # off workers included -- completes before the release runs.
        self._lock_conn = None

    @contextmanager
    def _own_connection(self):
        """A pooled connection of this call's own, whose statements the server bounds.

        The pool timeout bounds checkout, and this transaction-local
        ``statement_timeout`` bounds execution, so a cancelled sweep's drained
        worker cannot sit on one statement past
        :data:`SWEEP_STATEMENT_TIMEOUT_SECONDS` while the transport is alive.
        Transaction-local, so the connection returns to the pool unaltered.
        The API reads (get/list) keep the ordinary checkout: they run on the
        request path, which has its own semantics.
        """

        with self._db.connection() as conn:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(SWEEP_STATEMENT_TIMEOUT_SECONDS * 1000)),),
            )
            yield conn

    @contextmanager
    def _sweep_connection(self):
        """The lock's own session while a sweep holds it, else an own pooled connection.

        Sweep-path reads and writes ride the very connection the sweep lock is
        held on (issue #128): the lock is session-level, so if the process dies
        mid-sweep the server drops the writes' session *and* the lock together
        -- no write of a dead sweep can land after another replica has acquired
        the lock. Safe to share because the indexer runs its store calls one at
        a time and every sweep-path statement is a single statement, fine under
        the autocommit that session already uses; its ``statement_timeout`` is
        session-set by the lock helper, so no transaction-local set is needed
        (under autocommit it would be a no-op anyway). The lock-less targeted
        entry points (the webhook's ``reindex``/``mark_removed_one``) never
        take this path while a sweep runs: they use :meth:`_own_connection`,
        so they work concurrently with a sweep and stay out of the lock
        connection's lifecycle. Without a lock held (the store used directly,
        as in tests) this is exactly :meth:`_own_connection`.
        """

        conn = self._lock_conn
        if conn is not None:
            yield conn
            return
        with self._own_connection() as conn:
            yield conn

    def known(self, source_id: str) -> list[KnownArtifact]:
        with self._sweep_connection() as conn:
            rows = conn.execute(
                "SELECT repository, digest, tags, status, removed_at IS NOT NULL AS removed"
                " FROM collab_cog_artifacts WHERE source_id = %s",
                (source_id,),
            ).fetchall()
        return [
            KnownArtifact(
                repository=row["repository"],
                digest=row["digest"],
                tags=tuple(row["tags"] or ()),
                status=row["status"],
                removed=row["removed"],
            )
            for row in rows
        ]

    def upsert(self, artifact: CogArtifact, *, targeted: bool = False) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        if artifact.status not in STATUSES:
            raise ValueError(f"unknown catalog status {artifact.status!r}")
        require_aware(artifact.pushed_at, "pushed_at")
        require_aware(artifact.indexed_at, "indexed_at")
        # `indexed_at` is the server's clock: rows compare across replicas.
        # The card goes in as jsonb verbatim -- the reader's dict, structure
        # preserved -- and `removed_at` is cleared because a row being
        # (re)written was just seen in the registry. A targeted upsert (the
        # webhook's lock-less reindex) takes its own connection rather than a
        # running sweep's lock session -- see _sweep_connection (issue #128).
        connection = self._own_connection if targeted else self._sweep_connection
        try:
            with connection() as conn:
                conn.execute(
                    """
                INSERT INTO collab_cog_artifacts (
                    source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type,
                    status, card, cog_id, name, version, kind, publisher, manifest_schema, read_errors, removed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (source_id, repository, digest) DO UPDATE SET
                    host = EXCLUDED.host,
                    tags = EXCLUDED.tags,
                    pushed_at = EXCLUDED.pushed_at,
                    indexed_at = now(),
                    manifest_media_type = EXCLUDED.manifest_media_type,
                    status = EXCLUDED.status,
                    card = EXCLUDED.card,
                    cog_id = EXCLUDED.cog_id,
                    name = EXCLUDED.name,
                    version = EXCLUDED.version,
                    kind = EXCLUDED.kind,
                    publisher = EXCLUDED.publisher,
                    manifest_schema = EXCLUDED.manifest_schema,
                    read_errors = EXCLUDED.read_errors,
                    removed_at = NULL
                    """,
                    (
                        artifact.source_id,
                        artifact.host,
                        artifact.repository,
                        artifact.digest,
                        sorted(set(artifact.tags)),
                        artifact.pushed_at,
                        artifact.manifest_media_type,
                        artifact.status,
                        Jsonb(artifact.card) if artifact.card is not None else None,
                        artifact.cog_id,
                        artifact.name,
                        artifact.version,
                        artifact.kind,
                        artifact.publisher,
                        artifact.manifest_schema,
                        Jsonb(list(artifact.read_errors)),
                    ),
                )
        except psycopg.DataError as exc:
            # This row's *content* is what the server refused (NUL in a jsonb
            # string is the known case) -- an availability problem it is not,
            # and the two must fail differently: the indexer records a data
            # error against the artifact and continues, while an outage
            # aborts its sweep. Class name only; the server's message quotes
            # the offending value.
            raise CogCatalogDataError(type(exc).__name__) from exc

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        require_aware(pushed_at, "pushed_at")
        with self._sweep_connection() as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_artifacts
                SET tags = %s,
                    pushed_at = COALESCE(%s, pushed_at),
                    removed_at = NULL
                WHERE source_id = %s AND repository = %s AND digest = %s
                RETURNING digest
                """,
                (sorted(set(tags)), pushed_at, source_id, repository, digest),
            ).fetchone()
        return row is not None

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
        from psycopg.types.json import Jsonb

        # The present set travels as one jsonb document ({repo: [digest, ...]})
        # and is unnested server-side, so a source with thousands of artifacts
        # is one statement rather than one per repository, and the whole
        # decision is one snapshot. Repositories whose listing failed travel
        # as an array and are excluded from the UPDATE outright.
        document = {repo: sorted(set(digests)) for repo, digests in present.items()}
        shielded = sorted(set(excluding))
        with self._sweep_connection() as conn:
            row = conn.execute(
                """
                WITH present AS (
                    SELECT repo.key AS repository, digest.value #>> '{}' AS digest
                    FROM jsonb_each(%s) AS repo,
                         jsonb_array_elements(repo.value) AS digest
                ),
                marked AS (
                    UPDATE collab_cog_artifacts a
                    SET removed_at = now()
                    WHERE a.source_id = %s
                      AND a.removed_at IS NULL
                      AND NOT (a.repository = ANY(%s))
                      AND NOT EXISTS (
                          SELECT 1 FROM present p
                          WHERE p.repository = a.repository AND p.digest = a.digest
                      )
                    RETURNING 1
                )
                SELECT count(*) AS n FROM marked
                """,
                (Jsonb(document), source_id, shielded),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        # A targeted entry point (webhook delete), never the sweep's: it takes
        # its own bounded connection so it works concurrently with a sweep
        # instead of riding -- and dying with -- the lock session (issue #128).
        with self._own_connection() as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_artifacts SET removed_at = now()
                WHERE source_id = %s AND repository = %s AND digest = %s AND removed_at IS NULL
                RETURNING digest
                """,
                (source_id, repository, digest),
            ).fetchone()
        return row is not None

    def sweep_lock(self) -> AbstractContextManager[bool]:
        return _postgres_sweep_lock(self._db, store=self)

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM collab_cog_artifacts
                WHERE digest = %s
                  AND (%s::text IS NULL OR source_id = %s)
                  AND (%s::text IS NULL OR repository = %s)
                ORDER BY (removed_at IS NOT NULL), indexed_at DESC, source_id, repository
                LIMIT 1
                """,
                (digest, source_id, source_id, repository, repository),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def locations(self, digest) -> list[CogArtifact]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM collab_cog_artifacts WHERE digest = %s ORDER BY source_id, repository",
                (digest,),
            ).fetchall()
        return [_row_to_artifact(row) for row in rows]

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT) -> list[CogArtifact]:
        from psycopg.types.json import Jsonb

        limit = _bounded_limit(limit)
        filters = filters or CatalogFilter()
        needle = filters.containment()
        sql, params = self.current_query(filters, needle, limit)
        with self._db.connection() as conn:
            rows = conn.execute(sql, [Jsonb(p) if isinstance(p, dict) else p for p in params]).fetchall()
        return [_row_to_artifact(row) for row in rows]

    @staticmethod
    def current_query(filters: CatalogFilter, needle: dict[str, Any] | None, limit: int) -> tuple[str, list[Any]]:
        """The ``list_current`` statement, exposed so a live test can EXPLAIN it.

        The card filter is a single ``card @> %s`` -- the operator the GIN
        index (``jsonb_path_ops``) exists for. Everything else is an equality
        on an indexed or cheap column.
        """

        clauses = ["removed_at IS NULL", f"status = '{STATUS_INDEXED}'", "cog_id IS NOT NULL"]
        params: list[Any] = []
        for column, value in (
            ("kind", filters.kind),
            ("publisher", filters.publisher),
            ("source_id", filters.source_id),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        if needle is not None:
            clauses.append("card @> %s")
            params.append(needle)
        params.append(limit)
        sql = f"""
            SELECT DISTINCT ON (cog_id) {_COLUMNS}
            FROM collab_cog_artifacts
            WHERE {" AND ".join(clauses)}
            ORDER BY cog_id, pushed_at DESC NULLS LAST, indexed_at DESC, source_id, repository, digest
            LIMIT %s
        """
        return sql, params

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM collab_cog_artifacts
                WHERE cog_id = %s AND status = '{STATUS_INDEXED}' AND (%s OR removed_at IS NULL)
                ORDER BY pushed_at DESC NULLS LAST, indexed_at DESC, source_id, repository, digest
                """,
                (cog_id, include_removed),
            ).fetchall()
        return [_row_to_artifact(row) for row in rows]


@contextmanager
def _postgres_sweep_lock(db, store: PostgresCogCatalogStore | None = None):
    """``pg_try_advisory_lock(COG_INDEX_LOCK_KEY)`` on one pooled connection, held for the block.

    The lock is **session**-level, not transaction-level, and the connection
    is switched to autocommit for the duration so that no transaction (and no
    snapshot) stays open while the sweep talks to registries for minutes. The
    connection is one pool slot the sweep occupies; autocommit is restored
    before it goes back to the pool so the next borrower sees the transaction
    semantics every other store relies on. If the process dies mid-sweep the
    server releases the lock with the connection -- and with it every sweep
    write still in flight, because while the lock is held the connection is
    published to ``store`` for the sweep-path reads and writes to ride
    (issue #128; see :meth:`PostgresCogCatalogStore._sweep_connection`).
    Publication is withdrawn before the unlock, so nothing can pick the
    connection up once the lock is gone; a losing acquisition never publishes
    and never withdraws another session's publication.
    """

    with db.connection() as conn:
        original_autocommit = conn.autocommit
        acquired = False
        # The connection goes back to the pool only if every step of taking
        # the lock and giving it back is known to have succeeded. Anything
        # else -- the acquire statement failing (did the server take the lock
        # before the error?), the unlock failing (the session still holds
        # it), the RESET or the autocommit restore failing (the next borrower
        # would inherit a 20 s statement timeout, or autocommit) -- leaves
        # the session's state uncertain, and an uncertain session must not
        # be handed to the next borrower. Closing it makes the pool discard
        # it and open a fresh one; the server releases any lock with it.
        clean = False
        try:
            conn.autocommit = True
            # Session-set (autocommit makes a transaction-local set a no-op)
            # and RESET below before the connection returns: the acquire and
            # release statements are trivial, so this only matters when the
            # server itself has stopped answering them promptly -- exactly
            # when an unbounded statement would stall a cancelled sweep's
            # drain for its whole deadline.
            conn.execute(f"SET statement_timeout = '{int(SWEEP_STATEMENT_TIMEOUT_SECONDS * 1000)}ms'")
            row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (COG_INDEX_LOCK_KEY,)).fetchone()
            acquired = bool(row and row["locked"])
            if acquired and store is not None:
                store._lock_conn = conn
            try:
                yield acquired
            finally:
                if store is not None and store._lock_conn is conn:
                    store._lock_conn = None
                if not conn.closed:
                    if acquired:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (COG_INDEX_LOCK_KEY,))
                    conn.execute("RESET statement_timeout")
                    conn.autocommit = original_autocommit
                    clean = True
        finally:
            if not clean and not conn.closed:
                logger.error("cog_index_lock_connection_discarded", extra={"acquired": acquired})
                with suppress(Exception):
                    conn.close()


def card_search_fields(card: Mapping[str, Any]) -> dict[str, str | None]:
    """The search-key columns derived from a card.

    ``cog_id``, ``name``, ``version``, ``kind``, ``publisher``, ``manifest_schema``.

    Kept next to the store so the indexer and any backfill agree on which
    card keys the columns mirror. ``version`` is stringified because the
    profile may declare it as a number and the column is text.
    """

    def text(value: Any) -> str | None:
        if value is None or isinstance(value, (dict, list)):
            return None
        return value if isinstance(value, str) else json.dumps(value)

    return {
        "cog_id": text(card.get("id")),
        "name": text(card.get("name")),
        "version": text(card.get("version")),
        "kind": text(card.get("kind")),
        "publisher": text(card.get("publisher")),
        "manifest_schema": text(card.get("manifest_schema")),
    }
