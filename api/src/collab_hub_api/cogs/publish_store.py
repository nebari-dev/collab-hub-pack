"""What publishing through the Hub keeps in the database (issue #180).

Two small relations, both created by migration 14 of
:mod:`..frames.collab_schema` and never by this module:

- ``collab_cog_repositories`` -- **who owns a repository published through
  the Hub**. One row per repository path, written by the first manifest the
  Hub accepts for it (an upload alone claims nothing) and never changed
  after: the owner is the publisher's organization, and later pushes need
  membership of it. The rows are also what extends the publish source's
  enumeration, so that source needs no configured repository list.
- ``collab_cog_upload_sessions`` -- **an upload in progress**. A client
  uploads a blob over several requests that may land on different replicas,
  so the session lives here: the Hub's own session id (the only one a client
  ever sees), who opened it, and the backing registry's session URL, which
  never leaves the Hub.

Three backends, as for the catalog: Postgres over the shared pool, in memory
for tests and single-process development, and one that refuses. Every
Postgres call goes through :func:`~.deadline.bounded_connection`, so it is
bounded by the request budget.
"""

from __future__ import annotations

import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .deadline import bounded_connection

UPLOAD_ID_PREFIX = "up-"
UPLOAD_SESSION_TTL_SECONDS = 3600
"""How long an upload may stay open between its first request and its last."""

MAX_UPLOAD_SESSIONS_PER_USER = 64
"""Open uploads one user may hold; opening past it drops their oldest.

A client pushing a bundle opens one session per layer, a few at a time. The
cap bounds what a looping or hostile client can make the table (and the
backing registry's own session store) hold.
"""

SWEEP_INTERVAL_SECONDS = 300.0


class PublishStoreUnavailableError(RuntimeError):
    """Raised when publishing state is needed but no backend is configured."""


def new_upload_id() -> str:
    """An upload session id: one URL-safe path segment, unguessable."""

    return UPLOAD_ID_PREFIX + secrets.token_hex(16)


@dataclass(frozen=True)
class RepositoryRecord:
    """A repository first published through the Hub, and the organization that owns it."""

    repository: str
    source_id: str
    owner_org_id: str | None
    created_by: str


@dataclass(frozen=True)
class UploadSession:
    """One upload in progress. ``upstream_location`` is the backing registry's and is never sent to a client."""

    id: str
    user_id: str
    repository: str
    source_id: str
    upstream_location: str
    received: int = 0


class PublishStore(ABC):
    """Repository ownership and upload sessions."""

    @abstractmethod
    def get_repository(self, repository: str) -> RepositoryRecord | None:
        raise NotImplementedError

    @abstractmethod
    def claim_repository(
        self, repository: str, *, source_id: str, owner_org_id: str | None, created_by: str
    ) -> RepositoryRecord:
        """Record the repository's owner if it has none, and return the record that stands.

        First writer wins, atomically: two organizations publishing a new
        repository at once get one owner, and the loser reads the winner's
        record and must check it.
        """

        raise NotImplementedError

    @abstractmethod
    def published_repositories(self, source_id: str) -> list[str]:
        """The repositories published through the Hub into this source, sorted."""

        raise NotImplementedError

    @abstractmethod
    def open_upload(
        self, *, upload_id: str, user_id: str, repository: str, source_id: str, upstream_location: str
    ) -> UploadSession:
        """Record a new upload session; drops expired ones and the user's oldest past the cap."""

        raise NotImplementedError

    @abstractmethod
    def get_upload(self, upload_id: str, *, user_id: str, repository: str) -> UploadSession | None:
        """The live session with this id, **if it is this user's and for this repository**."""

        raise NotImplementedError

    @abstractmethod
    def advance_upload(self, upload_id: str, *, expected_received: int, received: int, upstream_location: str) -> bool:
        """Move a session forward after a chunk was forwarded. ``False`` if it had moved or gone meanwhile."""

        raise NotImplementedError

    @abstractmethod
    def close_upload(self, upload_id: str) -> None:
        """Forget a session (completed or cancelled). Idempotent."""

        raise NotImplementedError


class UnavailablePublishStore(PublishStore):
    """Used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> PublishStoreUnavailableError:
        return PublishStoreUnavailableError("Cog publishing storage is not configured")

    def get_repository(self, repository):
        raise self._refuse()

    def claim_repository(self, repository, *, source_id, owner_org_id, created_by):
        raise self._refuse()

    def published_repositories(self, source_id):
        raise self._refuse()

    def open_upload(self, **_kwargs):
        raise self._refuse()

    def get_upload(self, upload_id, *, user_id, repository):
        raise self._refuse()

    def advance_upload(self, upload_id, *, expected_received, received, upstream_location):
        raise self._refuse()

    def close_upload(self, upload_id):
        raise self._refuse()


@dataclass
class InMemoryPublishStore(PublishStore):
    """Process-local store for tests and single-process development; same semantics as Postgres."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _repositories: dict[str, RepositoryRecord] = field(default_factory=dict)
    _uploads: dict[str, tuple[UploadSession, datetime, datetime]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_repository(self, repository):
        with self._lock:
            return self._repositories.get(repository)

    def claim_repository(self, repository, *, source_id, owner_org_id, created_by):
        with self._lock:
            return self._repositories.setdefault(
                repository,
                RepositoryRecord(
                    repository=repository, source_id=source_id, owner_org_id=owner_org_id, created_by=created_by
                ),
            )

    def published_repositories(self, source_id):
        with self._lock:
            return sorted(name for name, record in self._repositories.items() if record.source_id == source_id)

    def _purge(self, now: datetime) -> None:
        for key in [key for key, (_session, _created, expires) in self._uploads.items() if expires <= now]:
            del self._uploads[key]

    def open_upload(self, *, upload_id, user_id, repository, source_id, upstream_location):
        now = self.clock()
        session = UploadSession(
            id=upload_id,
            user_id=user_id,
            repository=repository,
            source_id=source_id,
            upstream_location=upstream_location,
        )
        with self._lock:
            self._purge(now)
            self._uploads[upload_id] = (session, now, now + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS))
            mine = sorted(
                (
                    (created, key)
                    for key, (stored, created, _expires) in self._uploads.items()
                    if stored.user_id == user_id
                ),
            )
            for _created, key in mine[:-MAX_UPLOAD_SESSIONS_PER_USER]:
                del self._uploads[key]
        return session

    def get_upload(self, upload_id, *, user_id, repository):
        with self._lock:
            self._purge(self.clock())
            stored = self._uploads.get(upload_id)
            if stored is None or stored[0].user_id != user_id or stored[0].repository != repository:
                return None
            return stored[0]

    def advance_upload(self, upload_id, *, expected_received, received, upstream_location):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is None or stored[0].received != expected_received:
                return False
            session, created, expires = stored
            self._uploads[upload_id] = (
                replace(session, received=received, upstream_location=upstream_location),
                created,
                expires,
            )
            return True

    def close_upload(self, upload_id):
        with self._lock:
            self._uploads.pop(upload_id, None)


def _repository_from_row(row) -> RepositoryRecord:
    return RepositoryRecord(
        repository=row["repository"],
        source_id=row["source_id"],
        owner_org_id=row["owner_org_id"],
        created_by=row["created_by"],
    )


def _upload_from_row(row) -> UploadSession:
    return UploadSession(
        id=row["id"],
        user_id=row["user_id"],
        repository=row["repository"],
        source_id=row["source_id"],
        upstream_location=row["upstream_location"],
        received=int(row["received"]),
    )


UPLOAD_OPEN_LOCK_CLASS = int.from_bytes(b"cup1", "big")
"""First key of the advisory lock serializing one user's upload opening and pruning (second: ``hashtext(user)``)."""

_UPLOAD_COLUMNS = "id, user_id, repository, source_id, upstream_location, received"


class PostgresPublishStore(PublishStore):
    """The two tables of migration 14, over the shared pool. Carries no DDL."""

    def __init__(self, db):
        self._db = db
        self._last_sweep = time.monotonic()

    def get_repository(self, repository):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                "SELECT repository, source_id, owner_org_id, created_by FROM collab_cog_repositories"
                " WHERE repository = %s",
                (repository,),
            ).fetchone()
        return _repository_from_row(row) if row else None

    def claim_repository(self, repository, *, source_id, owner_org_id, created_by):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                INSERT INTO collab_cog_repositories (repository, source_id, owner_org_id, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repository) DO NOTHING
                """,
                (repository, source_id, owner_org_id, created_by),
            )
            # Read back in the same transaction: whoever inserted first, this
            # is the record that stands, and the caller checks it.
            row = conn.execute(
                "SELECT repository, source_id, owner_org_id, created_by FROM collab_cog_repositories"
                " WHERE repository = %s",
                (repository,),
            ).fetchone()
        return _repository_from_row(row)

    def published_repositories(self, source_id):
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT repository FROM collab_cog_repositories WHERE source_id = %s",
                (source_id,),
            ).fetchall()
        return sorted(row["repository"] for row in rows)

    def open_upload(self, *, upload_id, user_id, repository, source_id, upstream_location):
        with bounded_connection(self._db) as conn:
            # One user's opening and pruning are serialized, so concurrent
            # opens cannot each prune before the other commits and leave the
            # user over the cap. Released with the transaction.
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, user_id))
            conn.execute("DELETE FROM collab_cog_upload_sessions WHERE expires_at <= now()")
            row = conn.execute(
                f"""
                INSERT INTO collab_cog_upload_sessions
                    (id, user_id, repository, source_id, upstream_location, expires_at)
                VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (upload_id, user_id, repository, source_id, upstream_location, UPLOAD_SESSION_TTL_SECONDS),
            ).fetchone()
            conn.execute(
                """
                DELETE FROM collab_cog_upload_sessions
                WHERE user_id = %s AND id NOT IN (
                    SELECT id FROM collab_cog_upload_sessions
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                )
                """,
                (user_id, user_id, MAX_UPLOAD_SESSIONS_PER_USER),
            )
        return _upload_from_row(row)

    def get_upload(self, upload_id, *, user_id, repository):
        with bounded_connection(self._db) as conn:
            now = time.monotonic()
            if now - self._last_sweep >= SWEEP_INTERVAL_SECONDS:
                self._last_sweep = now
                conn.execute("DELETE FROM collab_cog_upload_sessions WHERE expires_at <= now()")
            row = conn.execute(
                f"""
                SELECT {_UPLOAD_COLUMNS} FROM collab_cog_upload_sessions
                WHERE id = %s AND user_id = %s AND repository = %s AND expires_at > now()
                """,
                (upload_id, user_id, repository),
            ).fetchone()
        return _upload_from_row(row) if row else None

    def advance_upload(self, upload_id, *, expected_received, received, upstream_location):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET received = %s, upstream_location = %s
                WHERE id = %s AND received = %s AND expires_at > now()
                RETURNING id
                """,
                (received, upstream_location, upload_id, expected_received),
            ).fetchone()
        return row is not None

    def close_upload(self, upload_id):
        with bounded_connection(self._db) as conn:
            conn.execute("DELETE FROM collab_cog_upload_sessions WHERE id = %s", (upload_id,))
