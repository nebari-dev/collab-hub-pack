"""Usage-statistics storage backends.

Usage capture has two halves:

- **Seen users** — every authenticated request upserts the caller into a
  per-tenant user roster (identity, email, first/last seen). Recording is
  best-effort and throttled so the auth path never gains a per-request write.
- **Usage events** — client-reported activity the hub cannot observe itself
  (today: ``chat_created`` from the desktop app). Events are append-only rows,
  aggregated by the usage endpoints alongside the existing frame history and
  active-state tables.

Backend semantics mirror ``history.py``: usage is a relational feature that
rides the shared ``frames.postgres`` URL. ``InMemory`` exists only for
tests/dev; with no DB configured the builder returns ``UnavailableUsageStore``.
On the unavailable store, best-effort writes (``record_user_seen``) are no-ops
while event recording and all reads raise ``UsageUnavailableError`` (→ 503).
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .history import cap_detail

# Minimum seconds between persisted ``last_seen`` refreshes for one caller.
# Auth runs on every request; without a throttle each request would cost an
# extra Postgres write. First sight (or a changed email) always writes.
SEEN_WRITE_INTERVAL_SECONDS = 300.0


class UsageUnavailableError(RuntimeError):
    """Raised when usage statistics are requested but no backend is configured."""

    pass


@dataclass(frozen=True)
class UsageUser:
    """One authenticated user seen by the API within a tenant."""

    user: str
    email: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class UsageEventCount:
    """Aggregated count of one client-reported event kind for one user."""

    event: str
    user: str
    count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UsageStore(ABC):
    """Tenant-scoped roster of authenticated users plus client usage events."""

    @abstractmethod
    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Upsert one authenticated caller into the seen-user roster.

        Called from the auth path, so implementations must stay cheap and may
        throttle repeat writes for the same caller.
        """

        raise NotImplementedError

    @abstractmethod
    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Append one client-reported usage event."""

        raise NotImplementedError

    @abstractmethod
    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return every seen user in the tenant, most recently seen first."""

        raise NotImplementedError

    @abstractmethod
    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Return per-user counts of each event kind within a time window."""

        raise NotImplementedError


class UnavailableUsageStore(UsageStore):
    """Store returned when no shared frames Postgres is configured.

    Seen-user recording is a no-op (best-effort relative to auth), while event
    recording and reads raise ``UsageUnavailableError`` so the usage endpoints
    return 503. This is the only off state — there is no per-feature toggle.
    """

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """No-op: recording is best-effort and no DB is configured."""

        return None

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Reject event writes because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")


@dataclass
class _SeenUser:
    email: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class _Event:
    org_id: str
    workspace_id: str
    user: str
    event: str
    detail: dict | None
    created_at: datetime


class InMemoryUsageStore(UsageStore):
    """Process-local usage store for tests and narrow dev scenarios.

    Unthrottled: every ``record_user_seen`` refreshes ``last_seen`` so tests
    stay deterministic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._users: dict[tuple[str, str, str], _SeenUser] = {}
        self._events: list[_Event] = []

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Upsert one caller into process-local memory."""

        now = _now()
        with self._lock:
            existing = self._users.get((org_id, workspace_id, user))
            if existing is None:
                self._users[(org_id, workspace_id, user)] = _SeenUser(
                    email=email,
                    first_seen=now,
                    last_seen=now,
                )
            else:
                existing.last_seen = now
                if email:
                    existing.email = email

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Append one usage event to process-local memory."""

        entry = _Event(
            org_id=org_id,
            workspace_id=workspace_id,
            user=user,
            event=event,
            detail=cap_detail(detail),
            created_at=_now(),
        )
        with self._lock:
            self._events.append(entry)

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return the tenant's seen users from process-local memory."""

        with self._lock:
            users = [
                UsageUser(
                    user=user,
                    email=record.email,
                    first_seen=record.first_seen,
                    last_seen=record.last_seen,
                )
                for (item_org, item_workspace, user), record in self._users.items()
                if item_org == org_id and item_workspace == workspace_id
            ]
        users.sort(key=lambda u: u.last_seen, reverse=True)
        return users

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Aggregate matching events from process-local memory."""

        counts: dict[tuple[str, str], int] = {}
        with self._lock:
            for entry in self._events:
                if entry.org_id != org_id or entry.workspace_id != workspace_id:
                    continue
                if since is not None and entry.created_at < since:
                    continue
                if until is not None and entry.created_at >= until:
                    continue
                key = (entry.event, entry.user)
                counts[key] = counts.get(key, 0) + 1
        return sorted(
            (UsageEventCount(event=event, user=user, count=count) for (event, user), count in counts.items()),
            key=lambda c: (c.event, -c.count, c.user),
        )


class PostgresUsageStore(UsageStore):
    """Postgres-backed usage store for production-style deployments.

    Mirrors ``PostgresFrameHistoryStore``: the database lives outside the pack;
    the pack only needs a connection URL and creates its tables if missing.
    ``record_user_seen`` throttles repeat writes per caller so the auth path
    stays cheap under request bursts.
    """

    def __init__(self, database_url: str, auto_migrate: bool = False):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgresUsageStore requires psycopg") from exc

        self.database_url = database_url
        self.psycopg = psycopg
        self.dict_row = dict_row
        self._seen_lock = threading.Lock()
        # (org, workspace, user, email) -> monotonic seconds of the last write.
        # Keying on the email means a changed claim writes through immediately.
        self._seen_writes: dict[tuple[str, str, str, str | None], float] = {}
        if auto_migrate:
            self._ensure_schema()

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Upsert one caller's roster row, at most once per throttle window."""

        key = (org_id, workspace_id, user, email)
        now = time.monotonic()
        with self._seen_lock:
            last = self._seen_writes.get(key)
            if last is not None and now - last < SEEN_WRITE_INTERVAL_SECONDS:
                return
            self._seen_writes[key] = now
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO frames_server_usage_users (
                        org_id, workspace_id, user_id, email, first_seen, last_seen
                    )
                    VALUES (%s, %s, %s, %s, now(), now())
                    ON CONFLICT (org_id, workspace_id, user_id)
                    DO UPDATE SET
                        last_seen = now(),
                        email = COALESCE(EXCLUDED.email, frames_server_usage_users.email)
                    """,
                    (org_id, workspace_id, user, email),
                )
        except Exception:
            # Allow an immediate retry on the next request instead of silently
            # skipping this caller for a whole throttle window.
            with self._seen_lock:
                self._seen_writes.pop(key, None)
            raise

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Insert one usage event into Postgres."""

        capped = cap_detail(detail)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO frames_server_usage_events (
                    id, org_id, workspace_id, user_id, event, detail
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4().hex,
                    org_id,
                    workspace_id,
                    user,
                    event,
                    self.psycopg.types.json.Jsonb(capped) if capped is not None else None,
                ),
            )

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return the tenant's seen users from Postgres, most recent first."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, email, first_seen, last_seen
                FROM frames_server_usage_users
                WHERE org_id = %s AND workspace_id = %s
                ORDER BY last_seen DESC, user_id
                """,
                (org_id, workspace_id),
            ).fetchall()
        return [
            UsageUser(
                user=row["user_id"],
                email=row["email"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
            )
            for row in rows
        ]

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Aggregate matching events in Postgres."""

        params: list[object] = [org_id, workspace_id]
        window_clause = ""
        if since is not None:
            window_clause += " AND created_at >= %s"
            params.append(since)
        if until is not None:
            window_clause += " AND created_at < %s"
            params.append(until)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT event, user_id, count(*) AS count
                FROM frames_server_usage_events
                WHERE org_id = %s AND workspace_id = %s{window_clause}
                GROUP BY event, user_id
                ORDER BY event, count DESC, user_id
                """,
                tuple(params),
            ).fetchall()
        return [
            UsageEventCount(event=row["event"], user=row["user_id"], count=row["count"])
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_usage_users (
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    user_id      text NOT NULL,
                    email        text,
                    first_seen   timestamptz NOT NULL DEFAULT now(),
                    last_seen    timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (org_id, workspace_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_usage_events (
                    id           text PRIMARY KEY,
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    user_id      text NOT NULL,
                    event        text NOT NULL,
                    detail       jsonb,
                    created_at   timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS frames_server_usage_events_lookup
                ON frames_server_usage_events (
                    org_id, workspace_id, created_at DESC
                )
                """
            )

    def _connect(self):
        return self.psycopg.connect(self.database_url, row_factory=self.dict_row)
