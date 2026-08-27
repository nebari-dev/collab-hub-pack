"""Startup-path coverage for the Postgres stores' auto-migration.

The API test suite runs everything else on the in-memory backends, so nothing
here exercised ``auto_migrate=True`` construction — the configuration real
deployments use. A refactor once swallowed
``PostgresActiveFrameStore._ensure_schema`` into an adjacent method, and pods
with auto-migration enabled would have crashed at startup with
``AttributeError``. These tests construct every Postgres store against a
stubbed pooled database and assert the migration DDL actually runs — and that
no store ever opens a direct ``psycopg.connect`` (issue #58: everything goes
through the shared connection pool).

The stub keeps this a fast startup-path test that needs no database. Verifying
the DDL against a real server (that the statements are valid Postgres and are
idempotent across restarts) needs a real-Postgres harness, which CI does not
have yet; that is tracked separately. Pool behavior under saturation is covered
against a real pool in ``test_postgres_pool.py``.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from collab_hub_api.frames.active_state import PostgresActiveFrameStore  # noqa: E402
from collab_hub_api.frames.groups import PostgresFrameGroupStore  # noqa: E402
from collab_hub_api.frames.history import PostgresFrameHistoryStore  # noqa: E402
from collab_hub_api.frames.usage import PostgresUsageStore  # noqa: E402
from collab_hub_api.tasks.store import PostgresTaskStore  # noqa: E402


class FakeConnection:
    """A stand-in connection recording every executed statement."""

    def __init__(self, executed: list[str]):
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._executed.append(" ".join(str(sql).split()))
        return self


class FakeDatabase:
    """A stand-in for ``db.PostgresDatabase`` handing out fake pooled connections."""

    database_url = "postgresql://stub/example"

    def __init__(self, executed: list[str]):
        self._executed = executed

    def connection(self, timeout=None):
        return FakeConnection(self._executed)

    def best_effort_connection(self):
        # The same connection; on the real pool this one has a near-zero
        # acquisition budget (used by the auth-path usage write).
        return FakeConnection(self._executed)


@pytest.mark.parametrize(
    "store_cls",
    [
        PostgresActiveFrameStore,
        PostgresFrameGroupStore,
        PostgresFrameHistoryStore,
        PostgresUsageStore,
        PostgresTaskStore,
    ],
)
def test_auto_migration_runs_schema_ddl_at_construction(monkeypatch, store_cls):
    executed: list[str] = []

    def forbid_direct_connect(*args, **kwargs):
        raise AssertionError(f"{store_cls.__name__} opened a direct psycopg.connect instead of using the pool")

    monkeypatch.setattr(psycopg, "connect", forbid_direct_connect)

    store_cls(FakeDatabase(executed), auto_migrate=True)

    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in executed), (
        f"{store_cls.__name__} did not run its schema migration on construction"
    )
