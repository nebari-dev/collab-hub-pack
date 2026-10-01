"""A pooled-database double for code that writes through ``audited()``.

Extracted from ``test_operator_foundation`` when a second suite needed the same
harness: two hand-copied doubles of the transaction contract would be one
psycopg change away from disagreeing about what the real connection does, and
the point of the double is that it behaves like the pooled context manager.

``rows`` scripts what reads answer, in order. An entry that is a list is a
result *set*: ``fetchall()`` returns it and ``fetchone()`` returns its first
row, which is how a real cursor behaves. Anything else is a single row.

The audit insert still answers with its own generated id regardless, because
every caller depends on that and none of them should have to script it.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg import pq  # noqa: E402

__all__ = [
    "FakeAuditConnection",
    "FakeAuditDatabase",
    "FakeConnectionInfo",
    "FakeTransaction",
]


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnectionInfo:
    def __init__(self):
        self.transaction_status = pq.TransactionStatus.INTRANS


class FakeAuditConnection:
    """Records statements and the transaction outcome, like the pooled CM."""

    # psycopg's AdaptContext surface, so psycopg.sql composables render
    # against the double the way they would against a real connection
    # (connection=None simply means "no connection settings": UTF-8).
    connection = None
    adapters = psycopg.adapters
    # A real cursor reports how many rows the last write touched; the double
    # answers "one", which is what a write that was not suppressed reports.
    rowcount = 1

    def __init__(self, rows=()):
        self.statements: list[tuple[str, tuple | None]] = []
        self.outcome: str | None = None
        self.info = FakeConnectionInfo()
        self._pending: dict | None = None
        # Shared with the database when it hands its own list over, so a script
        # is answered in order across connections, as code that reads on one
        # connection and writes on another sees it.
        self._rows = rows if isinstance(rows, list) else list(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc_info):
        # psycopg_pool's connection() context manager: commit on clean exit,
        # rollback when the body raised.
        self.outcome = "rollback" if exc_type else "commit"
        return False

    def transaction(self):
        return FakeTransaction(self)

    def cursor(self):
        # The fake doubles as its own cursor: execute/fetch live here anyway.
        return self

    def execute(self, sql, params=None):
        # psycopg is handed bytes for a rendered composable; record the text
        # the server would actually receive.
        sql = sql.decode() if isinstance(sql, (bytes, bytearray)) else str(sql)
        self.statements.append((" ".join(sql.split()), params))
        if "INSERT INTO collab_audit_events" in str(sql):
            self._pending = {"id": len(self.statements)}
        elif self._rows:
            self._pending = self._rows.pop(0)
        else:
            self._pending = None
        return self

    def fetchone(self):
        if isinstance(self._pending, list):
            return self._pending[0] if self._pending else None
        return self._pending

    def fetchall(self):
        if isinstance(self._pending, list):
            return list(self._pending)
        return [] if self._pending is None else [self._pending]


class FakeAuditDatabase:
    def __init__(self, rows=()):
        self.connections: list[FakeAuditConnection] = []
        self._rows = list(rows)

    def connection(self, timeout=None):
        conn = FakeAuditConnection(rows=self._rows)
        self.connections.append(conn)
        return conn
