"""What publishing through the Hub keeps in the database (issue #180).

Two small relations, both created by migration 14 of
:mod:`..frames.collab_schema` and never by this module:

- ``collab_cog_repositories`` -- **who owns a repository published through
  the Hub**. One row per repository path. A row starts as a *reservation*,
  taken just before a manifest is forwarded to the registry, and becomes
  ownership (``committed``) only once the registry has accepted that
  manifest; from then on it never changes, and later pushes need membership
  of the owning organization. A reservation is released when the registry
  has definitely refused every manifest forwarded under it (publishes of one
  organization share it, and it counts them); one whose outcome is unknown
  simply expires, and until then only the same organization can take it
  again. Only committed rows are ownership, and only committed rows extend
  the publish source's enumeration.
- ``collab_cog_upload_sessions`` -- **an upload in progress**. A client
  uploads a blob over several requests that may land on different replicas,
  so the session lives here: the Hub's own session id (the only one a client
  ever sees), who opened it, and the backing registry's session URL, which
  never leaves the Hub. The row is written *before* the registry is asked to
  open its session, so the per-user cap holds before anything exists
  upstream; one request at a time holds the session's *lease* while it
  forwards bytes; and a session past its expiry is kept until its registry
  session has been cancelled (see :meth:`PublishStore.claim_stale_uploads`).

Three backends, as for the catalog: Postgres over the shared pool, in memory
for tests and single-process development, and one that refuses. Every
Postgres call goes through :func:`~.deadline.bounded_connection`, so it is
bounded by the request budget.
"""

from __future__ import annotations

import secrets
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .deadline import bounded_connection

UPLOAD_ID_PREFIX = "up-"
UPLOAD_SESSION_TTL_SECONDS = 3600
"""How long an upload may stay open between its first request and its last."""

UPLOAD_HARD_AGE_SECONDS = 24 * 3600
"""How long a session whose registry-side cancellation keeps failing is kept before it is dropped regardless."""

MAX_UPLOAD_SESSIONS_PER_USER = 64
"""Open uploads one user may hold; opening past it retires their oldest.

A client pushing a bundle opens one session per layer, a few at a time. The
cap bounds what a looping or hostile client can make the table (and the
backing registry's own session store) hold.
"""

MAX_UPLOAD_ROWS_PER_USER = 2 * MAX_UPLOAD_SESSIONS_PER_USER
"""Live sessions plus retired ones still awaiting cancellation at the registry; past it, opening is refused."""

RESERVATION_ID_PREFIX = "rsv-"


class PublishStoreUnavailableError(RuntimeError):
    """Raised when publishing state is needed but no backend is configured."""


class UploadLimitError(RuntimeError):
    """This user has too many sessions the registry has not let go of yet; nothing was opened."""


def new_reservation_id() -> str:
    """What identifies one reservation of a repository name, so only those who hold it can release it."""

    return RESERVATION_ID_PREFIX + secrets.token_hex(16)


def new_upload_id() -> str:
    """An upload session id: one URL-safe path segment, unguessable."""

    return UPLOAD_ID_PREFIX + secrets.token_hex(16)


@dataclass(frozen=True)
class RepositoryRecord:
    """A repository's row: ownership once ``committed``, a reservation of the name until then."""

    repository: str
    source_id: str
    owner_org_id: str | None
    created_by: str
    committed: bool = True
    reservation: str | None = None
    """The reservation a :meth:`PublishStore.reserve_repository` call now holds a share of; ``None`` otherwise.

    A caller holds the name exactly when the record it got back is committed
    (then ownership decides) or carries a reservation.
    """


@dataclass(frozen=True)
class UploadSession:
    """One upload in progress. ``upstream_location`` is the backing registry's and is never sent to a client."""

    id: str
    user_id: str
    repository: str
    source_id: str
    upstream_location: str | None
    received: int = 0
    lease: datetime | None = None
    """The lease this holder took, as stored: what proves a later write or release is the holder's."""


class PublishStore(ABC):
    """Repository ownership and upload sessions."""

    # -- repositories ----------------------------------------------------------

    @abstractmethod
    def get_repository(self, repository: str) -> RepositoryRecord | None:
        """The repository's **committed** owner record; a reservation is not ownership."""

        raise NotImplementedError

    @abstractmethod
    def reserve_repository(
        self, repository: str, *, source_id: str, owner_org_id: str | None, created_by: str, ttl_seconds: float
    ) -> RepositoryRecord:
        """Reserve the name for this organization, and return the record that stands.

        Atomic. A committed record is returned untouched. With none, the
        reservation is taken, or taken over once the holder's has expired;
        an organization that already holds a live one *joins* it (a retry, a
        colleague publishing at the same moment), which counts one more
        holder and extends it. Either way the record returned carries the
        ``reservation``. A live reservation held by another organization is
        returned as it is, uncommitted and with no ``reservation``: the name
        is not this caller's to write to yet.
        """

        raise NotImplementedError

    @abstractmethod
    def commit_repository(
        self, repository: str, *, source_id: str, owner_org_id: str | None, created_by: str
    ) -> RepositoryRecord:
        """Make this organization the repository's owner; return the record that stands.

        Called once the registry has accepted a manifest, and only then.
        Already committed: untouched, and the caller checks whose it is.
        Otherwise the row -- this organization's reservation, no row at all,
        or a reservation that changed hands after this one expired -- becomes
        this organization's ownership: the registry holds its manifest, so
        the first accepted manifest decides, and what was accepted is always
        enumerated.
        """

        raise NotImplementedError

    @abstractmethod
    def release_repository(self, repository: str, *, reservation: str) -> None:
        """Give back one hold on this reservation (the registry refused the manifest); the last one frees the name.

        Counted, because publishes of one organization share a reservation:
        a refused publish must not free the name under a concurrent publish
        of the same organization that is still waiting for the registry's
        answer. A hold that is never given back (an unknown outcome) keeps
        the name until the reservation expires. Nothing happens if the
        reservation is no longer the one that stands, or was committed.
        """

        raise NotImplementedError

    @abstractmethod
    def published_repositories(self, source_id: str) -> list[str]:
        """The repositories published through the Hub into this source -- committed ones only -- sorted."""

        raise NotImplementedError

    # -- upload sessions ---------------------------------------------------------

    @abstractmethod
    def open_upload(self, *, upload_id: str, user_id: str, repository: str, source_id: str) -> UploadSession:
        """Reserve a session slot, **before** anything is opened at the registry.

        Serialized per user. Live sessions past the cap are retired, oldest
        first: retired, not deleted -- they stay until
        :meth:`claim_stale_uploads` has had them cancelled at the registry.
        Raises :class:`UploadLimitError` when the user's rows (live and
        retired) are already at :data:`MAX_UPLOAD_ROWS_PER_USER`. The row has
        no registry location until :meth:`attach_upload`, and is unknown to
        :meth:`get_upload` until then.
        """

        raise NotImplementedError

    @abstractmethod
    def attach_upload(self, upload_id: str, upstream_location: str) -> bool:
        """Record where the registry opened the session. ``False`` if the row is gone."""

        raise NotImplementedError

    @abstractmethod
    def get_upload(self, upload_id: str, *, user_id: str, repository: str) -> UploadSession | None:
        """The live session with this id, **if it is this user's and for this repository**."""

        raise NotImplementedError

    @abstractmethod
    def lease_upload(
        self, upload_id: str, *, user_id: str, repository: str, lease_seconds: float
    ) -> UploadSession | None:
        """Take the session's mutation lease. ``None`` if it is held (or the session is not there).

        The lease is what lets exactly one request, on any replica, forward
        bytes to -- or close, or cancel -- a registry session at a time. It
        expires by itself, so a holder that died does not strand the session.
        """

        raise NotImplementedError

    @abstractmethod
    def advance_upload(
        self, upload_id: str, *, lease: datetime, expected_received: int, received: int, upstream_location: str
    ) -> bool:
        """Move a leased session forward after a chunk was forwarded, and give the lease back.

        ``False`` -- and nothing changed -- if the lease is no longer this
        holder's, the session had moved, or it expired meanwhile.
        """

        raise NotImplementedError

    @abstractmethod
    def release_upload(self, upload_id: str, *, lease: datetime) -> None:
        """Give the lease back, if it is still this holder's. Idempotent."""

        raise NotImplementedError

    @abstractmethod
    def retire_upload(self, upload_id: str) -> None:
        """End a session whose registry session could not be cancelled: unusable from now, kept for cleanup.

        The row expires at once and its lease is dropped, so
        :meth:`claim_stale_uploads` picks it up and the cancellation is tried
        again. Idempotent.
        """

        raise NotImplementedError

    @abstractmethod
    def close_upload(self, upload_id: str) -> None:
        """Forget a session whose registry session is finished, cancelled, or was never opened. Idempotent."""

        raise NotImplementedError

    @abstractmethod
    def claim_stale_uploads(
        self, *, limit: int, lease_seconds: float, user_id: str | None = None
    ) -> list[UploadSession]:
        """Lease up to ``limit`` expired sessions (of one user, if given) for cancellation at the registry.

        The caller cancels each one upstream and only then calls
        :meth:`close_upload`; one whose cancellation fails is left, and is
        claimable again when the lease runs out. Sessions older than
        :data:`UPLOAD_HARD_AGE_SECONDS` are dropped here outright.
        """

        raise NotImplementedError


class UnavailablePublishStore(PublishStore):
    """Used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> PublishStoreUnavailableError:
        return PublishStoreUnavailableError("Cog publishing storage is not configured")

    def get_repository(self, repository):
        raise self._refuse()

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, ttl_seconds):
        raise self._refuse()

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        raise self._refuse()

    def release_repository(self, repository, *, reservation):
        raise self._refuse()

    def published_repositories(self, source_id):
        raise self._refuse()

    def open_upload(self, **_kwargs):
        raise self._refuse()

    def attach_upload(self, upload_id, upstream_location):
        raise self._refuse()

    def get_upload(self, upload_id, *, user_id, repository):
        raise self._refuse()

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        raise self._refuse()

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        raise self._refuse()

    def release_upload(self, upload_id, *, lease):
        raise self._refuse()

    def retire_upload(self, upload_id):
        raise self._refuse()

    def close_upload(self, upload_id):
        raise self._refuse()

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        raise self._refuse()


@dataclass
class _StoredUpload:
    session: UploadSession
    created: datetime
    expires: datetime
    leased_until: datetime | None = None


@dataclass
class InMemoryPublishStore(PublishStore):
    """Process-local store for tests and single-process development; same semantics as Postgres."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _repositories: dict[str, tuple[RepositoryRecord, datetime, int]] = field(default_factory=dict)
    _uploads: dict[str, _StoredUpload] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- repositories ----------------------------------------------------------

    def get_repository(self, repository):
        with self._lock:
            stored = self._repositories.get(repository)
            return stored[0] if stored is not None and stored[0].committed else None

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, ttl_seconds):
        now = self.clock()
        with self._lock:
            reservation, holders = new_reservation_id(), 1
            stored = self._repositories.get(repository)
            if stored is not None:
                record, until, held = stored
                if record.committed:
                    return record
                if until > now:
                    if record.owner_org_id != owner_org_id:
                        return replace(record, reservation=None)
                    reservation, holders = record.reservation, held + 1
            record = RepositoryRecord(
                repository=repository,
                source_id=source_id,
                owner_org_id=owner_org_id,
                created_by=created_by,
                committed=False,
                reservation=reservation,
            )
            self._repositories[repository] = (record, now + timedelta(seconds=ttl_seconds), holders)
            return record

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        now = self.clock()
        with self._lock:
            stored = self._repositories.get(repository)
            if stored is not None and stored[0].committed:
                return stored[0]
            record = RepositoryRecord(
                repository=repository,
                source_id=source_id,
                owner_org_id=owner_org_id,
                created_by=created_by,
                committed=True,
            )
            self._repositories[repository] = (record, now, 0)
            return record

    def release_repository(self, repository, *, reservation):
        with self._lock:
            stored = self._repositories.get(repository)
            if stored is None or stored[0].committed or stored[0].reservation != reservation:
                return
            record, until, holders = stored
            if holders > 1:
                self._repositories[repository] = (record, until, holders - 1)
            else:
                del self._repositories[repository]

    def published_repositories(self, source_id):
        with self._lock:
            return sorted(
                name
                for name, (record, _until, _holders) in self._repositories.items()
                if record.source_id == source_id and record.committed
            )

    # -- upload sessions ---------------------------------------------------------

    def open_upload(self, *, upload_id, user_id, repository, source_id):
        now = self.clock()
        session = UploadSession(
            id=upload_id, user_id=user_id, repository=repository, source_id=source_id, upstream_location=None
        )
        with self._lock:
            mine = [stored for stored in self._uploads.values() if stored.session.user_id == user_id]
            if len(mine) >= MAX_UPLOAD_ROWS_PER_USER:
                raise UploadLimitError("too many upload sessions are still being cleaned up")
            live = sorted(
                (stored for stored in mine if stored.expires > now),
                key=lambda stored: (stored.created, stored.session.id),
            )
            for stored in live[: max(len(live) - (MAX_UPLOAD_SESSIONS_PER_USER - 1), 0)]:
                stored.expires = now
            self._uploads[upload_id] = _StoredUpload(
                session=session, created=now, expires=now + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS)
            )
        return session

    def attach_upload(self, upload_id, upstream_location):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is None:
                return False
            stored.session = replace(stored.session, upstream_location=upstream_location)
            return True

    def _live(self, upload_id, user_id, repository, now) -> _StoredUpload | None:
        stored = self._uploads.get(upload_id)
        if (
            stored is None
            or stored.expires <= now
            or stored.session.upstream_location is None
            or stored.session.user_id != user_id
            or stored.session.repository != repository
        ):
            return None
        return stored

    def get_upload(self, upload_id, *, user_id, repository):
        with self._lock:
            stored = self._live(upload_id, user_id, repository, self.clock())
            return stored.session if stored is not None else None

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        now = self.clock()
        with self._lock:
            stored = self._live(upload_id, user_id, repository, now)
            if stored is None or (stored.leased_until is not None and stored.leased_until > now):
                return None
            stored.leased_until = now + timedelta(seconds=lease_seconds)
            return replace(stored.session, lease=stored.leased_until)

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if (
                stored is None
                or stored.leased_until != lease
                or stored.session.received != expected_received
                or stored.expires <= self.clock()
            ):
                return False
            stored.session = replace(stored.session, received=received, upstream_location=upstream_location)
            stored.leased_until = None
            return True

    def release_upload(self, upload_id, *, lease):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is not None and stored.leased_until == lease:
                stored.leased_until = None

    def retire_upload(self, upload_id):
        now = self.clock()
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is not None:
                stored.expires = min(stored.expires, now)
                stored.leased_until = None

    def close_upload(self, upload_id):
        with self._lock:
            self._uploads.pop(upload_id, None)

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        now = self.clock()
        hard = now - timedelta(seconds=UPLOAD_HARD_AGE_SECONDS)
        claimed = []
        with self._lock:
            for key in [key for key, stored in self._uploads.items() if stored.created <= hard]:
                del self._uploads[key]
            stale = sorted(
                (
                    stored
                    for stored in self._uploads.values()
                    if stored.expires <= now
                    and (stored.leased_until is None or stored.leased_until <= now)
                    and (user_id is None or stored.session.user_id == user_id)
                ),
                key=lambda stored: (stored.expires, stored.session.id),
            )
            for stored in stale[: max(limit, 0)]:
                stored.leased_until = now + timedelta(seconds=lease_seconds)
                claimed.append(replace(stored.session, lease=stored.leased_until))
        return claimed


def _repository_from_row(row) -> RepositoryRecord:
    return RepositoryRecord(
        repository=row["repository"],
        source_id=row["source_id"],
        owner_org_id=row["owner_org_id"],
        created_by=row["created_by"],
        reservation=row["reservation"],
        committed=bool(row["committed"]),
    )


def _held_by(record: RepositoryRecord, owner_org_id: str | None) -> RepositoryRecord:
    """The record as the organization that just asked for the name may see it.

    The reservation statement takes or joins every reservation it is allowed
    to, so an uncommitted row of this organization's is one it now holds;
    anybody else's reservation id is not theirs to be shown.
    """

    held = not record.committed and record.owner_org_id == owner_org_id
    return record if held else replace(record, reservation=None)


def _upload_from_row(row) -> UploadSession:
    return UploadSession(
        id=row["id"],
        user_id=row["user_id"],
        repository=row["repository"],
        source_id=row["source_id"],
        upstream_location=row["upstream_location"],
        received=int(row["received"]),
        lease=row["leased_until"],
    )


UPLOAD_OPEN_LOCK_CLASS = int.from_bytes(b"cup1", "big")
"""First key of the advisory lock serializing one user's upload opening and pruning (second: ``hashtext(user)``)."""

_REPOSITORY_COLUMNS = "repository, source_id, owner_org_id, created_by, committed, reservation"
_UPLOAD_COLUMNS = "id, user_id, repository, source_id, upstream_location, received, leased_until"


class PostgresPublishStore(PublishStore):
    """The two tables of migration 14, over the shared pool. Carries no DDL."""

    def __init__(self, db):
        self._db = db

    # -- repositories ----------------------------------------------------------

    def _read_repository(self, conn, repository):
        row = conn.execute(
            f"SELECT {_REPOSITORY_COLUMNS} FROM collab_cog_repositories WHERE repository = %s",
            (repository,),
        ).fetchone()
        return _repository_from_row(row) if row else None

    def get_repository(self, repository):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                f"SELECT {_REPOSITORY_COLUMNS} FROM collab_cog_repositories WHERE repository = %s AND committed",
                (repository,),
            ).fetchone()
        return _repository_from_row(row) if row else None

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, ttl_seconds):
        with bounded_connection(self._db) as conn:
            # One statement decides: a committed row, and a live reservation
            # of another organization's, are both left exactly as they are; a
            # live one of this organization's is joined; anything else is
            # taken afresh.
            conn.execute(
                """
                INSERT INTO collab_cog_repositories
                    (repository, source_id, owner_org_id, created_by, committed, reservation, holders, reserved_until)
                VALUES (%s, %s, %s, %s, false, %s, 1, now() + make_interval(secs => %s))
                ON CONFLICT (repository) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    owner_org_id = EXCLUDED.owner_org_id,
                    created_by = EXCLUDED.created_by,
                    reservation = CASE WHEN collab_cog_repositories.reserved_until > now()
                                       THEN collab_cog_repositories.reservation ELSE EXCLUDED.reservation END,
                    holders = CASE WHEN collab_cog_repositories.reserved_until > now()
                                   THEN collab_cog_repositories.holders + 1 ELSE 1 END,
                    reserved_until = EXCLUDED.reserved_until
                WHERE NOT collab_cog_repositories.committed
                  AND (collab_cog_repositories.reserved_until <= now()
                       OR collab_cog_repositories.owner_org_id IS NOT DISTINCT FROM EXCLUDED.owner_org_id)
                """,
                (repository, source_id, owner_org_id, created_by, new_reservation_id(), ttl_seconds),
            )
            # Read back in the same transaction: whoever holds the name now,
            # this is the record that stands, and the caller checks it.
            return _held_by(self._read_repository(conn, repository), owner_org_id)

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                INSERT INTO collab_cog_repositories
                    (repository, source_id, owner_org_id, created_by, committed, reserved_until)
                VALUES (%s, %s, %s, %s, true, now())
                ON CONFLICT (repository) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    owner_org_id = EXCLUDED.owner_org_id,
                    created_by = EXCLUDED.created_by,
                    committed = true,
                    reservation = NULL
                WHERE NOT collab_cog_repositories.committed
                """,
                (repository, source_id, owner_org_id, created_by),
            )
            return self._read_repository(conn, repository)

    def release_repository(self, repository, *, reservation):
        with bounded_connection(self._db) as conn:
            # Counted down first, under the row's lock, and deleted only at
            # zero: two releases at once cannot both see "one holder left".
            conn.execute(
                """
                UPDATE collab_cog_repositories SET holders = holders - 1
                WHERE repository = %s AND NOT committed AND reservation = %s
                """,
                (repository, reservation),
            )
            conn.execute(
                """
                DELETE FROM collab_cog_repositories
                WHERE repository = %s AND NOT committed AND reservation = %s AND holders <= 0
                """,
                (repository, reservation),
            )

    def published_repositories(self, source_id):
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT repository FROM collab_cog_repositories WHERE source_id = %s AND committed",
                (source_id,),
            ).fetchall()
        return sorted(row["repository"] for row in rows)

    # -- upload sessions ---------------------------------------------------------

    def open_upload(self, *, upload_id, user_id, repository, source_id):
        with bounded_connection(self._db) as conn:
            # One user's opening and retiring are serialized, so concurrent
            # opens cannot each count before the other commits and leave the
            # user over the cap. Released with the transaction.
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, user_id))
            held = conn.execute(
                "SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if int(held["n"]) >= MAX_UPLOAD_ROWS_PER_USER:
                raise UploadLimitError("too many upload sessions are still being cleaned up")
            # Retired, not deleted: the registry still holds a session for
            # each, and the row is what remembers where to cancel it.
            conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET expires_at = now()
                WHERE user_id = %s AND expires_at > now() AND id NOT IN (
                    SELECT id FROM collab_cog_upload_sessions
                    WHERE user_id = %s AND expires_at > now()
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                )
                """,
                (user_id, user_id, MAX_UPLOAD_SESSIONS_PER_USER - 1),
            )
            row = conn.execute(
                f"""
                INSERT INTO collab_cog_upload_sessions (id, user_id, repository, source_id, expires_at)
                VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (upload_id, user_id, repository, source_id, UPLOAD_SESSION_TTL_SECONDS),
            ).fetchone()
        return _upload_from_row(row)

    def attach_upload(self, upload_id, upstream_location):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                "UPDATE collab_cog_upload_sessions SET upstream_location = %s WHERE id = %s RETURNING id",
                (upstream_location, upload_id),
            ).fetchone()
        return row is not None

    def get_upload(self, upload_id, *, user_id, repository):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                f"""
                SELECT {_UPLOAD_COLUMNS} FROM collab_cog_upload_sessions
                WHERE id = %s AND user_id = %s AND repository = %s
                  AND expires_at > now() AND upstream_location IS NOT NULL
                """,
                (upload_id, user_id, repository),
            ).fetchone()
        return _upload_from_row(row) if row else None

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        with bounded_connection(self._db) as conn:
            # Compare-and-set on the row: of two requests for one session, on
            # any two replicas, exactly one gets it.
            row = conn.execute(
                f"""
                UPDATE collab_cog_upload_sessions
                SET leased_until = clock_timestamp() + make_interval(secs => %s)
                WHERE id = %s AND user_id = %s AND repository = %s
                  AND expires_at > now() AND upstream_location IS NOT NULL
                  AND (leased_until IS NULL OR leased_until <= now())
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (lease_seconds, upload_id, user_id, repository),
            ).fetchone()
        return _upload_from_row(row) if row else None

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_upload_sessions
                SET received = %s, upstream_location = %s, leased_until = NULL
                WHERE id = %s AND leased_until = %s AND received = %s AND expires_at > now()
                RETURNING id
                """,
                (received, upstream_location, upload_id, lease, expected_received),
            ).fetchone()
        return row is not None

    def release_upload(self, upload_id, *, lease):
        with bounded_connection(self._db) as conn:
            conn.execute(
                "UPDATE collab_cog_upload_sessions SET leased_until = NULL WHERE id = %s AND leased_until = %s",
                (upload_id, lease),
            )

    def retire_upload(self, upload_id):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET expires_at = LEAST(expires_at, now()), leased_until = NULL
                WHERE id = %s
                """,
                (upload_id,),
            )

    def close_upload(self, upload_id):
        with bounded_connection(self._db) as conn:
            conn.execute("DELETE FROM collab_cog_upload_sessions WHERE id = %s", (upload_id,))

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        with bounded_connection(self._db) as conn:
            conn.execute(
                "DELETE FROM collab_cog_upload_sessions WHERE created_at <= now() - make_interval(secs => %s)",
                (UPLOAD_HARD_AGE_SECONDS,),
            )
            rows = conn.execute(
                f"""
                UPDATE collab_cog_upload_sessions
                SET leased_until = clock_timestamp() + make_interval(secs => %s)
                WHERE id IN (
                    SELECT id FROM collab_cog_upload_sessions
                    WHERE expires_at <= now() AND (leased_until IS NULL OR leased_until <= now())
                      AND (%s::text IS NULL OR user_id = %s)
                    ORDER BY expires_at, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (lease_seconds, user_id, user_id, max(limit, 0)),
            ).fetchall()
        return [_upload_from_row(row) for row in rows]
