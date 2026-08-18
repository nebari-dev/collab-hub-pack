"""Shared psycopg connection pooling for the Postgres-backed frames stores.

Every Postgres store used to open a fresh ``psycopg.connect(...)`` per method
call — a full TLS + SCRAM handshake (~3–15ms) in front of queries that often
cost well under a millisecond. This module gives the app one
``psycopg_pool.ConnectionPool`` per distinct database URL (normally just the
shared ``frames.postgres`` URL), shared by all stores on that URL and
lifecycle-managed by the app: opened at startup, closed at shutdown, and
checkable from the ``/health/db`` endpoint.

Failure semantics are deliberately explicit:

- Postgres down at startup does **not** crash the app (pools open without
  waiting); requests that need the database fail with ``PoolTimeout`` after
  the configured wait timeout, which the app maps to a 503.
- Pool exhaustion raises the same ``PoolTimeout`` after the same timeout —
  requests queue for a connection rather than stampeding Postgres.
- The queue of waiters is itself bounded (``max_waiting``): once it is full,
  further callers fail immediately with ``TooManyRequests`` instead of piling
  up behind a saturated pool. Both map to 503.
- Best-effort writes (usage capture during auth) check out through
  ``best_effort_connection()``: a near-zero acquisition budget, plus a deadline
  on the statement itself, because a database that answers the checkout and
  then takes its time answering the query would otherwise hold the thread for
  as long as it liked. Those writes also run off the request path entirely —
  see :mod:`.best_effort`.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10
DEFAULT_TIMEOUT_SECONDS = 5.0
# Cap on clients queued for a connection; beyond it callers fail fast with
# ``TooManyRequests`` instead of queueing behind an already saturated pool.
# 0 means "unbounded" (psycopg_pool's own default) and is discouraged.
DEFAULT_MAX_WAITING = 50

# Sanity bounds, mirrored by the app config validators and the helm schema.
POOL_SIZE_LIMIT = 500
TIMEOUT_SECONDS_LIMIT = 60.0
MAX_WAITING_LIMIT = 10_000

# Acquisition budget for best-effort writes: long enough to use a connection
# that is free right now, short enough that a saturated pool or a dead database
# costs nothing worth measuring.
BEST_EFFORT_TIMEOUT_SECONDS = 0.05

# Deadline for the statements of one best-effort write, applied by the server
# and transaction-local so the pooled connection goes back unaltered. Past it
# the server aborts the statement and the write is retried under the caller's
# backoff.
BEST_EFFORT_STATEMENT_TIMEOUT_SECONDS = 2.0

# If the server has neither answered nor honored its own statement_timeout by
# here, the client asks it to cancel. ``cancel_safe`` is documented as callable
# from another thread, which is what makes this usable as a watchdog.
BEST_EFFORT_CANCEL_AFTER_SECONDS = 3.0
BEST_EFFORT_CANCEL_TIMEOUT_SECONDS = 1.0


class _CancelWatchdog:
    """Cancels one stalled best-effort statement — and never a later one.

    The cancel has to come from another thread, because the thread that issued
    the statement is the one stuck waiting for it. That makes disarming a race:
    ``threading.Timer.cancel()`` only stops a callback that has not started
    yet, and a callback that *has* started can sit inside ``cancel_safe`` for
    its own timeout. If the checkout finished during that window the connection
    would already be back in the pool and handed to the next borrower, and the
    stale cancel would abort *that* caller's query instead.

    So the callback and the disarm take the same lock. Disarming either wins —
    and the callback becomes a no-op — or it waits out a cancel already in
    flight, bounded by ``BEST_EFFORT_CANCEL_TIMEOUT_SECONDS`` and paid on a
    background best-effort worker, never a request thread. Either way, by the
    time the connection goes back to the pool nothing can still touch it.
    """

    def __init__(self, conn, after_seconds: float, cancel_timeout_seconds: float):
        self._conn = conn
        self._cancel_timeout = cancel_timeout_seconds
        self._lock = threading.Lock()
        self._disarmed = False
        self.fired = False
        """Whether the watchdog reached the connection before being disarmed."""

        self._timer = threading.Timer(after_seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def disarm(self) -> None:
        """Guarantee the watchdog will not touch the connection again.

        Returns only once any callback already running has finished, so the
        caller may safely return the connection to the pool.
        """

        self._timer.cancel()
        with self._lock:
            self._disarmed = True

    def _fire(self) -> None:
        with self._lock:
            if self._disarmed:
                return
            self.fired = True
            try:
                self._conn.cancel_safe(timeout=self._cancel_timeout)
            except Exception:
                # A server that will not answer the query may not answer the
                # cancel either. Nothing further is safe to do from this
                # thread: closing the connection out from under the one
                # blocked on it is a use-after-free in libpq, so the bound
                # that always holds is that the blocked thread is a background
                # worker (see best_effort.BestEffortWriter).
                pass


# Shared advisory-lock key for the pre-existing `frames_server_*` and
# `nexus_task_*` startup DDL (issue #42). Bare `CREATE TABLE IF NOT EXISTS` is
# not concurrency-safe in Postgres: two replicas starting at the same instant
# can both pass the existence check and then race the catalog insert, so one
# pod dies at startup with a duplicate-key error on `pg_type`/`pg_class`.
# `replicaCount: 1` has masked this so far, but the chart supports autoscaling.
#
# One shared key serializes all five stores' schema creation against each
# other, which is deliberately coarse: none of this DDL is large or slow, it
# only ever runs once per replica at startup, and a single well-known key is
# simpler to reason about (and to grep for) than a family of near-identical
# ones. This mirrors `frames.collab_schema.COLLAB_SCHEMA_LOCK_KEY`, which uses
# the same `int.from_bytes(<8 ASCII bytes>, "big")` derivation so the constant
# stays readable; any distinct 64-bit value would do, and deriving it from a
# name just makes a collision with another advisory-lock user of the shared
# database unlikely.
FRAMES_SERVER_SCHEMA_LOCK_KEY = int.from_bytes(b"fsvrddl1", "big")


@contextmanager
def locked_schema_connection(db: "PostgresDatabase", lock_key: int = FRAMES_SERVER_SCHEMA_LOCK_KEY):
    """Check out a pooled connection with the schema advisory lock already held.

    Every Postgres store's ``_ensure_schema`` should open its ``with`` block on
    this instead of ``db.connection()`` directly. ``pg_advisory_xact_lock`` is
    taken as the connection's first statement, before any catalog access —
    including the guarded ``CREATE TABLE IF NOT EXISTS`` itself, which is
    exactly the statement that races under concurrent replica startup (issue
    #42). The lock is transaction-scoped (the ``_xact_`` variant), so it
    releases automatically on commit or rollback when the ``with`` block
    exits — no unlock bookkeeping, and no leak if the pod dies mid-migration.

    Whichever replica loses the race simply waits: it blocks on the lock, then
    runs its own ``CREATE TABLE IF NOT EXISTS`` against a catalog that already
    has the table, which is a no-op.
    """

    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        yield conn


def postgres_error_classes() -> tuple[type[Exception], ...]:
    """Exception types that mean "the frames database is unavailable" (→ 503).

    ``PoolTimeout`` (pool exhausted, or Postgres unreachable past the wait
    timeout) and ``TooManyRequests`` (waiter queue full) both subclass
    ``psycopg.OperationalError``; they are listed separately only for
    explicitness.
    """

    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout, TooManyRequests

    return (PoolTimeout, TooManyRequests, OperationalError)


class PostgresDatabase:
    """One lazily-opened connection pool for one database URL.

    The underlying pool is created **closed**, so building an app (or a test
    fixture) never spawns background connection workers; it opens on first use
    or eagerly at app startup via ``PostgresPools.open``. ``connection()``
    returns psycopg_pool's transaction-scoped context manager, so
    ``with db.connection() as conn:`` keeps the same commit/rollback-on-exit
    semantics the stores had with per-request ``psycopg.connect``.
    """

    def __init__(
        self,
        database_url: str,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_waiting: int = DEFAULT_MAX_WAITING,
    ):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError("Postgres-backed frames stores require psycopg[binary,pool]") from exc

        self.database_url = database_url
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._opened = False
        self._pool = ConnectionPool(
            database_url,
            open=False,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_seconds,
            max_waiting=max_waiting,
            kwargs={"row_factory": dict_row},
        )

    def connection(self, timeout: float | None = None):
        """Check out a pooled connection as a transaction-scoped context manager."""

        self.open()
        return self._pool.connection(timeout=timeout)

    @contextmanager
    def best_effort_connection(self):
        """Check out a connection for a write the caller can afford to lose.

        Bounded twice over, because the two halves fail differently:

        * **Acquisition** gets a near-zero budget instead of the full pool
          timeout, so a saturated pool or an unreachable database gives up in
          milliseconds. Never longer than the pool's own configured wait.
        * **Execution** gets a ``statement_timeout``, set transaction-locally so
          the connection returns to the pool unaltered, with a client-side
          cancel behind it if the server sits past even that. Without this a
          database that answered the checkout and then stalled would hold the
          calling thread indefinitely.

        The cancel is disarmed *inside* the checkout, and disarming waits out a
        callback already in flight, so a watchdog can never follow the
        connection into the next borrower's hands (see :class:`_CancelWatchdog`).
        """

        acquisition = min(BEST_EFFORT_TIMEOUT_SECONDS, self.timeout_seconds)
        with self.connection(timeout=acquisition) as conn:
            # Read at call time, not bound as defaults, so the timings stay
            # patchable for tests that need the watchdog to fire on demand.
            watchdog = _CancelWatchdog(
                conn,
                after_seconds=BEST_EFFORT_CANCEL_AFTER_SECONDS,
                cancel_timeout_seconds=BEST_EFFORT_CANCEL_TIMEOUT_SECONDS,
            )
            watchdog.start()
            try:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(int(BEST_EFFORT_STATEMENT_TIMEOUT_SECONDS * 1000)),),
                )
                yield conn
            finally:
                watchdog.disarm()

    def open(self) -> None:
        """Open the pool without waiting for connections to be established.

        Not waiting is what keeps a Postgres outage at startup from crashing
        the app: the pool's workers keep retrying in the background while
        ``connection()`` callers fail with ``PoolTimeout`` (→ 503).
        """

        if self._opened:
            return
        with self._lock:
            if not self._opened:
                self._pool.open(wait=False)
                self._opened = True

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self._pool.close()
                self._opened = False

    def stats(self) -> dict[str, int]:
        """psycopg_pool's own counters (size, available connections, waiters)."""

        return self._pool.get_stats()

    def check(self) -> None:
        """Round-trip one pooled connection; raises when Postgres is unreachable."""

        with self.connection() as conn:
            conn.execute("SELECT 1")


class PostgresPools:
    """Registry of one shared ``PostgresDatabase`` per distinct database URL.

    In the common deployment every relational feature rides the single shared
    ``frames.postgres`` URL, so this holds exactly one pool; a separate
    active-state URL gets its own. The app opens/closes the registry in its
    lifespan and reports it via ``/health/db``.
    """

    def __init__(
        self,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_waiting: int = DEFAULT_MAX_WAITING,
    ):
        self._min_size = min_size
        self._max_size = max_size
        self._timeout_seconds = timeout_seconds
        self._max_waiting = max_waiting
        self._lock = threading.Lock()
        self._databases: dict[str, PostgresDatabase] = {}

    def database(self, database_url: str) -> PostgresDatabase:
        """Return the shared pool for ``database_url``, creating it on first use."""

        with self._lock:
            db = self._databases.get(database_url)
            if db is None:
                db = PostgresDatabase(
                    database_url,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    timeout_seconds=self._timeout_seconds,
                    max_waiting=self._max_waiting,
                )
                self._databases[database_url] = db
            return db

    @property
    def configured(self) -> bool:
        return bool(self._databases)

    def open(self) -> None:
        for db in self._databases.values():
            db.open()

    def close(self) -> None:
        for db in self._databases.values():
            db.close()

    def check(self) -> list[dict[str, str]]:
        """Return one status entry per pool, without leaking connection URLs."""

        results: list[dict[str, str]] = []
        for index, db in enumerate(self._databases.values(), start=1):
            entry = {"pool": f"postgres-{index}", "status": "ok"}
            try:
                db.check()
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = type(exc).__name__
            results.append(entry)
        return results
