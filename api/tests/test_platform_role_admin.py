"""Granting and revoking the operator role from the admin panel.

Distinct from the identity-provider sync next door: that reconciles rows it
owns, this is a person deciding. The two must not fight, which is what the
``source`` column is for -- an admin's grant is ``manual`` and sync never
touches it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity  # noqa: E402
from collab_hub_api.frames.platform_role_admin import PostgresPlatformRoleAdmin  # noqa: E402

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


def test_granting_writes_a_hand_administered_row_and_records_it():
    """`manual`, so the provider sync leaves it alone -- see the module note."""

    db = FakeAuditDatabase()
    admin = PostgresPlatformRoleAdmin(db)

    admin.grant(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write = next(sql for sql, _ in statements(db) if "collab_platform_roles" in sql)
    assert "INSERT" in write and "ON CONFLICT (user_id) DO UPDATE" in write
    params = next(p for sql, p in statements(db) if "collab_platform_roles" in sql)
    assert "manual" in params

    actor, _label, action, target_type, target_id, _tl, org_id, _detail = audit_row(db)
    assert (actor, action, target_type, target_id, org_id) == (
        "u-1",
        "platform_role.grant",
        "user",
        "u-2",
        None,
    )


def test_revoking_records_its_own_action():
    db = FakeAuditDatabase()
    admin = PostgresPlatformRoleAdmin(db)

    admin.revoke(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write = next(sql for sql, _ in statements(db) if "collab_platform_roles" in sql)
    assert "UPDATE" in write
    _actor, _label, action, *_rest = audit_row(db)
    assert action == "platform_role.revoke"


def test_an_admin_revoke_reaches_provider_granted_rows_too():
    """Unlike sync, a person may revoke a row the provider created.

    Taking authority away must never be blocked by where it came from. The row
    stays `idp`, so if that person is still in the group their next sign-in
    restores it -- which is correct, and is why the panel says so.
    """

    db = FakeAuditDatabase()
    admin = PostgresPlatformRoleAdmin(db)

    admin.revoke(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write = next(sql for sql, _ in statements(db) if "collab_platform_roles" in sql)
    assert "source" not in write
