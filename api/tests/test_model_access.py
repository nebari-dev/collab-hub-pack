"""Granting and revoking model access, with its record.

Access is enforced at the serving gateway, which reads Keycloak group
membership. This service changes that membership and records what it did.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity  # noqa: E402
from collab_hub_api.frames.group_membership import GroupMembershipError  # noqa: E402
from collab_hub_api.frames.model_access import ModelAccessService  # noqa: E402

OPERATOR = AuthContext(
    user="u-1",
    home_org_id=None,
    workspace_id=WORKSPACE_DEFAULT,
    display=DisplayIdentity(name="Alice", email="alice@example.com"),
    org_role=None,
    platform_role="operator",
)


class StubMembership:
    configured = True

    def __init__(self, fails: Exception | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self._fails = fails

    def add_member(self, *, user_id, group_path):
        if self._fails:
            raise self._fails
        self.calls.append(("add", user_id, group_path))

    def remove_member(self, *, user_id, group_path):
        if self._fails:
            raise self._fails
        self.calls.append(("remove", user_id, group_path))


def audit_row(db):
    for conn in db.connections:
        for sql, params in conn.statements:
            if "INSERT INTO collab_audit_events" in sql:
                return params
    return None


def test_a_grant_changes_keycloak_and_records_what_it_did():
    db = FakeAuditDatabase()
    membership = StubMembership()
    service = ModelAccessService(db=db, membership=membership)

    service.grant(OPERATOR, user_id="u-2", group_path="/llm", user_label="bob@example.com")

    assert membership.calls == [("add", "u-2", "/llm")]
    actor, _label, action, target_type, target_id, _tl, org_id, detail = audit_row(db)
    assert (actor, action, target_type, target_id, org_id) == (
        "u-1",
        "service_access.grant",
        "user",
        "u-2",
        None,
    )
    assert detail.obj == {"group_path": "/llm"}


def test_a_revoke_records_its_own_action_rather_than_a_grant_with_a_flag():
    db = FakeAuditDatabase()
    membership = StubMembership()
    service = ModelAccessService(db=db, membership=membership)

    service.revoke(OPERATOR, user_id="u-2", group_path="/llm", user_label="bob@example.com")

    assert membership.calls == [("remove", "u-2", "/llm")]
    _actor, _label, action, *_rest = audit_row(db)
    assert action == "service_access.revoke"


def test_a_refused_membership_change_records_nothing():
    """A log that claims changes which did not happen is worse than no log."""

    db = FakeAuditDatabase()
    membership = StubMembership(fails=GroupMembershipError("Keycloak refused"))
    service = ModelAccessService(db=db, membership=membership)

    with pytest.raises(GroupMembershipError):
        service.grant(OPERATOR, user_id="u-2", group_path="/llm", user_label="bob@example.com")

    assert audit_row(db) is None
    assert all(conn.outcome == "rollback" for conn in db.connections)
