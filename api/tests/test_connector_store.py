"""The connector switch, against the database contract it actually uses."""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.auth import (  # noqa: E402
    WORKSPACE_DEFAULT,
    AuthContext,
    DisplayIdentity,
)
from collab_hub_api.frames.connector_store import PostgresConnectorStore  # noqa: E402

OPERATOR = AuthContext(
    user="u-1",
    home_org_id=None,
    workspace_id=WORKSPACE_DEFAULT,
    display=DisplayIdentity(name="Alice", email="alice@example.com"),
    org_role=None,
    platform_role="operator",
)


def statements(db):
    return [entry for conn in db.connections for entry in conn.statements]


def audit_row(db):
    for sql, params in statements(db):
        if "INSERT INTO collab_audit_events" in sql:
            return params
    return None


def test_only_switched_off_connectors_come_back():
    db = FakeAuditDatabase(rows=[[{"connector": "slack"}, {"connector": "github"}]])

    assert PostgresConnectorStore(db).disabled() == {"slack", "github"}
    (sql, _params) = statements(db)[0]
    assert "enabled = false" in sql


def test_nothing_switched_off_is_an_empty_set_rather_than_a_failure():
    db = FakeAuditDatabase(rows=[[]])

    assert PostgresConnectorStore(db).disabled() == set()


def test_a_database_without_the_table_has_switched_nothing_off():
    """Failing closed here would disable every connector on a deployment whose
    only fault is being one migration behind."""

    class MissingTable:
        def connection(self, timeout=None):
            raise psycopg.errors.UndefinedTable("relation does not exist")

    assert PostgresConnectorStore(MissingTable()).disabled() == set()


def test_switching_off_records_a_disable_against_the_connector():
    db = FakeAuditDatabase()

    PostgresConnectorStore(db).set_enabled(OPERATOR, connector="slack", enabled=False)

    write = next(sql for sql, _ in statements(db) if "collab_connector_state" in sql)
    assert "INSERT" in write and "ON CONFLICT (connector) DO UPDATE" in write

    actor, _label, action, target_type, target_id, _tl, org_id, _detail = audit_row(db)
    assert (actor, action, target_type, target_id, org_id) == (
        "u-1",
        "connector.disable",
        "connector",
        "slack",
        None,
    )


def test_switching_on_records_the_opposite_action():
    db = FakeAuditDatabase()

    PostgresConnectorStore(db).set_enabled(OPERATOR, connector="slack", enabled=True)

    _actor, _label, action, *_rest = audit_row(db)
    assert action == "connector.enable"
