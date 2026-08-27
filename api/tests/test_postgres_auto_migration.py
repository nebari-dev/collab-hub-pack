"""Startup-path coverage for the Postgres stores' auto-migration.

The API test suite runs everything else on the in-memory backends, so nothing
here exercised ``auto_migrate=True`` construction — the configuration real
deployments use. A refactor once swallowed
``PostgresActiveFrameStore._ensure_schema`` into an adjacent method, and pods
with auto-migration enabled would have crashed at startup with
``AttributeError``. These tests construct every Postgres store against a
stubbed connection and assert the migration DDL actually runs.
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
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda url, row_factory=None: FakeConnection(executed),
    )

    store_cls("postgresql://stub/example", auto_migrate=True)

    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in executed), (
        f"{store_cls.__name__} did not run its schema migration on construction"
    )
