"""The Cog catalog: where indexed artifacts live (issue #84).

One row per ``(source_id, repository, digest)`` in ``collab_cog_artifacts``,
created by migration version 7 of :mod:`..frames.collab_schema` -- never by
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
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager
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


class CogCatalogUnavailableError(RuntimeError):
    """Raised when the catalog is needed but no backend is configured."""


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
    def upsert(self, artifact: CogArtifact) -> None:
        """Insert the row or replace it whole; a replaced row is present again (``removed_at`` cleared)."""

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
    def mark_removed(self, source_id: str, present: Mapping[str, Iterable[str]]) -> int:
        """Set ``removed_at = now()`` on this source's present rows whose digest is not in ``present``.

        ``present`` maps repository -> digests enumerated this sweep. Returns
        the number of rows newly marked. Never deletes.
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
        when its connection drops.
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


def json_contains(document: Any, needle: Any) -> bool:
    """Postgres ``jsonb @>`` containment, for the in-memory store.

    Objects: every key of ``needle`` is in ``document`` with a contained value.
    Arrays: every element of ``needle`` is contained by *some* element of
    ``document`` (a scalar needle element matches a scalar document element).
    Scalars: equality.
    """

    if isinstance(needle, dict):
        return isinstance(document, dict) and all(
            k in document and json_contains(document[k], v) for k, v in needle.items()
        )
    if isinstance(needle, list):
        if not isinstance(document, list):
            # Postgres: a scalar array-contains a scalar, but not an array.
            return len(needle) == 1 and not isinstance(needle[0], (dict, list)) and document == needle[0]
        return all(any(json_contains(item, wanted) for item in document) for wanted in needle)
    return document == needle


class UnavailableCogCatalogStore(CogCatalogStore):
    """Store used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> CogCatalogUnavailableError:
        return CogCatalogUnavailableError("Cog catalog storage is not configured")

    def known(self, source_id: str) -> list[KnownArtifact]:
        raise self._refuse()

    def upsert(self, artifact: CogArtifact) -> None:
        raise self._refuse()

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        raise self._refuse()

    def mark_removed(self, source_id, present) -> int:
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

    def upsert(self, artifact: CogArtifact) -> None:
        if artifact.status not in STATUSES:
            raise ValueError(f"unknown catalog status {artifact.status!r}")
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

    def mark_removed(self, source_id, present) -> int:
        wanted = {repo: set(digests) for repo, digests in present.items()}
        now = datetime.now(UTC)
        marked = 0
        with self._lock:
            for key, row in list(self._rows.items()):
                if row.source_id != source_id or row.removed_at is not None:
                    continue
                if row.digest in wanted.get(row.repository, ()):
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
        # Present rows first, then most recently indexed.
        candidates.sort(key=lambda row: (row.removed_at is not None, _sort_key(row)))
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
    runner in :mod:`..frames.collab_schema` (version 7). A store that also
    emitted DDL would reintroduce the unlocked ``CREATE TABLE IF NOT EXISTS``
    race that runner exists to remove.
    """

    def __init__(self, db):
        self._db = db

    def known(self, source_id: str) -> list[KnownArtifact]:
        with self._db.connection() as conn:
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

    def upsert(self, artifact: CogArtifact) -> None:
        from psycopg.types.json import Jsonb

        if artifact.status not in STATUSES:
            raise ValueError(f"unknown catalog status {artifact.status!r}")
        # `indexed_at` is the server's clock: rows compare across replicas.
        # The card goes in as jsonb verbatim -- the reader's dict, structure
        # preserved -- and `removed_at` is cleared because a row being
        # (re)written was just seen in the registry.
        with self._db.connection() as conn:
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

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        with self._db.connection() as conn:
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

    def mark_removed(self, source_id, present) -> int:
        from psycopg.types.json import Jsonb

        # The present set travels as one jsonb document ({repo: [digest, ...]})
        # and is unnested server-side, so a source with thousands of artifacts
        # is one statement rather than one per repository, and the whole
        # decision is one snapshot.
        document = {repo: sorted(set(digests)) for repo, digests in present.items()}
        with self._db.connection() as conn:
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
                      AND NOT EXISTS (
                          SELECT 1 FROM present p
                          WHERE p.repository = a.repository AND p.digest = a.digest
                      )
                    RETURNING 1
                )
                SELECT count(*) AS n FROM marked
                """,
                (Jsonb(document), source_id),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        with self._db.connection() as conn:
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
        return _postgres_sweep_lock(self._db)

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM collab_cog_artifacts
                WHERE digest = %s
                  AND (%s::text IS NULL OR source_id = %s)
                  AND (%s::text IS NULL OR repository = %s)
                ORDER BY (removed_at IS NOT NULL), pushed_at DESC NULLS LAST, indexed_at DESC,
                         source_id, repository
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
def _postgres_sweep_lock(db):
    """``pg_try_advisory_lock(COG_INDEX_LOCK_KEY)`` on one pooled connection, held for the block.

    The lock is **session**-level, not transaction-level, and the connection
    is switched to autocommit for the duration so that no transaction (and no
    snapshot) stays open while the sweep talks to registries for minutes. The
    connection is one pool slot the sweep occupies; autocommit is restored
    before it goes back to the pool so the next borrower sees the transaction
    semantics every other store relies on. If the process dies mid-sweep the
    server releases the lock with the connection.
    """

    with db.connection() as conn:
        original_autocommit = conn.autocommit
        conn.autocommit = True
        acquired = False
        try:
            row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (COG_INDEX_LOCK_KEY,)).fetchone()
            acquired = bool(row and row["locked"])
            yield acquired
        finally:
            try:
                if acquired and not conn.closed:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (COG_INDEX_LOCK_KEY,))
            finally:
                if not conn.closed:
                    conn.autocommit = original_autocommit


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
