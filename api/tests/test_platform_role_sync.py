"""Platform-role sync: the identity provider owns who is an operator.

The hub holds a synced copy so that authorization stays a local read, and the
copy is reconciled at sign-in from the verified ID token's ``groups`` claim.
These tests drive the reconcile through its public interface and assert on what
lands in the database transaction, because that is the whole observable effect.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.auth import DisplayIdentity  # noqa: E402
from collab_hub_api.frames.orgs import (  # noqa: E402
    PLATFORM_ROLE_OPERATOR,
    InMemoryOrgStore,
)
from collab_hub_api.frames.platform_role_sync import (  # noqa: E402
    DisabledPlatformRoleSync,
    InMemoryPlatformRoleSync,
    PostgresPlatformRoleSync,
)

ADMIN_GROUP = "/hub-admins"
USER = "u-1"
DISPLAY = DisplayIdentity(name="Alice", email="alice@example.com", email_verified=True)


def _statements(db):
    """Every statement the reconcile issued, across however many connections."""

    return [entry for conn in db.connections for entry in conn.statements]


def _audit_insert(db):
    for sql, params in _statements(db):
        if "INSERT INTO collab_audit_events" in sql:
            return params
    return None


def test_a_member_of_the_admin_group_is_granted_the_operator_role():
    """The tracer bullet: claim says admin, no row exists, so a row appears."""

    db = FakeAuditDatabase(rows=[None])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY)

    assert role == PLATFORM_ROLE_OPERATOR
    assert all(conn.outcome == "commit" for conn in db.connections)
    assert any("collab_platform_roles" in sql and "INSERT" in sql for sql, _ in _statements(db))

    actor, actor_label, action, target_type, target_id, _target_label, org_id, _detail = _audit_insert(db)
    assert (actor, action, target_type, target_id, org_id) == (USER, "platform_role.grant", "user", USER, None)
    assert actor_label == "alice@example.com"


def test_a_synced_row_is_revoked_when_the_claim_no_longer_carries_the_group():
    """Sync removes as well as adds, or a dropped admin keeps their authority."""

    db = FakeAuditDatabase(rows=[{"role": PLATFORM_ROLE_OPERATOR, "status": "active", "source": "idp"}])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=["/everyone"], display=DISPLAY)

    assert role is None
    assert any("collab_platform_roles" in sql and "UPDATE" in sql for sql, _ in _statements(db))
    actor, _label, action, target_type, target_id, _tl, org_id, _detail = _audit_insert(db)
    assert (actor, action, target_type, target_id, org_id) == (USER, "platform_role.revoke", "user", USER, None)


def test_a_hand_administered_row_is_never_touched_by_sync():
    """The bootstrap operator is in no group, and must survive signing in."""

    db = FakeAuditDatabase(rows=[{"role": PLATFORM_ROLE_OPERATOR, "status": "active", "source": "manual"}])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=[], display=DISPLAY)

    assert role == PLATFORM_ROLE_OPERATOR
    assert _audit_insert(db) is None
    assert not any("UPDATE" in sql or "INSERT INTO collab_platform_roles" in sql for sql, _ in _statements(db))


def test_a_revoked_synced_row_is_granted_again_when_the_claim_returns():
    """Rejoining the group restores authority; the row is sync's to reuse."""

    db = FakeAuditDatabase(rows=[{"role": PLATFORM_ROLE_OPERATOR, "status": "revoked", "source": "idp"}])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY)

    assert role == PLATFORM_ROLE_OPERATOR
    _actor_id, _label, action, _tt, _ti, _tl, _org, _detail = _audit_insert(db)
    assert action == "platform_role.grant"
    # The row already exists and user_id is the primary key, so the grant has
    # to be an upsert. A plain insert passes against a double and raises a
    # unique violation against Postgres, which is the wrong place to find out.
    write = next(sql for sql, _ in _statements(db) if "collab_platform_roles" in sql and "INSERT" in sql)
    assert "ON CONFLICT (user_id) DO UPDATE" in write
    assert "WHERE collab_platform_roles.source = %s" in write


def test_a_hand_revoked_row_is_not_resurrected_by_the_claim():
    """A deliberate manual revocation outranks group membership."""

    db = FakeAuditDatabase(rows=[{"role": PLATFORM_ROLE_OPERATOR, "status": "revoked", "source": "manual"}])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY)

    assert role is None
    assert _audit_insert(db) is None


def test_an_unchanged_grant_writes_nothing_at_all():
    """Most sign-ins change nothing, and a log of "still an operator" is noise."""

    db = FakeAuditDatabase(rows=[{"role": PLATFORM_ROLE_OPERATOR, "status": "active", "source": "idp"}])
    sync = PostgresPlatformRoleSync(db, admin_group=ADMIN_GROUP)

    role = sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY)

    assert role == PLATFORM_ROLE_OPERATOR
    assert _audit_insert(db) is None
    assert all("SELECT" in sql for sql, _ in _statements(db))


def test_a_deployment_that_named_no_admin_group_syncs_nothing():
    """An unset group must not match nothing and revoke every synced row."""

    sync = DisabledPlatformRoleSync()

    assert sync.configured is False
    assert sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY) is None


# ---------------------------------------------------------------------------
# The in-memory pairing, so a deployment without Postgres syncs too
# ---------------------------------------------------------------------------


def test_the_in_memory_sync_grants_and_revokes_against_the_org_store():
    """Same decisions, no database. Memory deployments keep no audit log.

    ``audited()`` writes to Postgres and there is no in-memory audit table, so
    this pairing records nothing. That is the existing shape of every other
    memory-backed store here, not a gap this module introduces.
    """

    store = InMemoryOrgStore()
    sync = InMemoryPlatformRoleSync(store, admin_group=ADMIN_GROUP)

    assert sync.reconcile(user_id=USER, claim_groups=[ADMIN_GROUP], display=DISPLAY) == PLATFORM_ROLE_OPERATOR
    assert store.resolve_principal(USER).platform_role == PLATFORM_ROLE_OPERATOR

    assert sync.reconcile(user_id=USER, claim_groups=[], display=DISPLAY) is None
    assert store.resolve_principal(USER).platform_role is None


def test_the_in_memory_sync_leaves_a_hand_seeded_row_alone():
    """The bootstrap row is `manual` here too, for the same reason."""

    store = InMemoryOrgStore()
    store.set_platform_role(USER)
    sync = InMemoryPlatformRoleSync(store, admin_group=ADMIN_GROUP)

    assert sync.reconcile(user_id=USER, claim_groups=[], display=DISPLAY) == PLATFORM_ROLE_OPERATOR
    assert store.resolve_principal(USER).platform_role == PLATFORM_ROLE_OPERATOR
