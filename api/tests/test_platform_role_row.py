"""Reading a platform-role row as written, for the admin panel.

``resolve_principal`` deliberately collapses a revoked grant to "no role",
which is right on the auth path and wrong for a screen that has to explain what
revoking will do. This is the other read.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from collab_hub_api.frames.orgs import (  # noqa: E402
    OrgSchemaMissingError,
    PostgresOrgStore,
)


class Connection:
    def __init__(self, row):
        self._row = row
        self.statements: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))
        return self

    def fetchone(self):
        return self._row


class Database:
    def __init__(self, row):
        self.conn = Connection(row)

    def connection(self, timeout=None):
        return self.conn


def test_the_row_comes_back_with_the_source_that_decides_who_may_change_it():
    db = Database({"role": "operator", "status": "active", "source": "idp"})

    assert PostgresOrgStore(db).get_platform_role_row("u-1") == {
        "role": "operator",
        "status": "active",
        "source": "idp",
    }
    (sql, params) = db.conn.statements[0]
    assert "FROM collab_platform_roles WHERE user_id = %s" in sql
    assert params == ("u-1",)


def test_somebody_with_no_row_reads_as_none():
    assert PostgresOrgStore(Database(None)).get_platform_role_row("u-2") is None


def test_a_revoked_row_is_returned_as_written():
    """The panel needs the revoked row; the auth path is the one that hides it."""

    db = Database({"role": "operator", "status": "revoked", "source": "manual"})

    assert PostgresOrgStore(db).get_platform_role_row("u-1")["status"] == "revoked"


def test_a_database_without_the_schema_says_so():
    class MissingTable:
        def connection(self, timeout=None):
            raise psycopg.errors.UndefinedTable("relation does not exist")

    with pytest.raises(OrgSchemaMissingError):
        PostgresOrgStore(MissingTable()).get_platform_role_row("u-1")
