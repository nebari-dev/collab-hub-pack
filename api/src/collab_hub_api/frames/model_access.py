"""Changing who may use a model, and recording that it happened.

Access is enforced at the serving gateway, which reads Keycloak group
membership. Nothing here enforces anything: this changes the input that gateway
reads, and writes the hub's own record of having done so.

Ordering, and the gap it leaves
-------------------------------
The Keycloak call happens **inside** the audited transaction. So a refusal from
Keycloak rolls the transaction back and no audit row is written, which is the
important direction: a log that claims changes which did not happen is worse
than no log, because it is trusted.

The remaining gap is the other direction, and it is stated rather than hidden.
If Keycloak succeeds and the commit then fails -- the database goes away in the
window between the two -- the membership change stands with no record of it.
That cannot be closed by reordering, only by a durable intent record written
before the call and settled after it, which is the shape
``collab_service_access_grants`` already has for the invitation path. Adopting
it here is the right next step and is deliberately not bundled into this change.

What is recorded
----------------
The actor, the subject, the group, and which direction it went. Grant and
revoke are separate actions rather than one action with a flag in ``detail``,
because "who lost access" is a question asked with a runbook query after an
incident, and answering it should not require parsing JSON.
"""

from __future__ import annotations

from .audit import (
    AUDIT_ACTION_SERVICE_ACCESS_GRANT,
    AUDIT_ACTION_SERVICE_ACCESS_REVOKE,
    audited,
)
from .auth import AuthContext

__all__ = ["ModelAccessService"]


class ModelAccessService:
    """Grant and revoke one person's access to one model group."""

    def __init__(self, *, db, membership) -> None:
        self._db = db
        self._membership = membership

    @property
    def configured(self) -> bool:
        return bool(self._db) and getattr(self._membership, "configured", False)

    def list_members(self, group_path: str):
        """Who is currently in *group_path*.

        A read, so it writes no audit row: recording who looked at a list would
        bury the rows that say who changed something, which is what this log is
        read for.
        """

        return self._membership.list_members(group_path)

    def grant(self, actor: AuthContext, *, user_id: str, group_path: str, user_label: str | None = None) -> None:
        self._change(
            actor,
            action=AUDIT_ACTION_SERVICE_ACCESS_GRANT,
            user_id=user_id,
            group_path=group_path,
            user_label=user_label,
            apply=self._membership.add_member,
        )

    def revoke(self, actor: AuthContext, *, user_id: str, group_path: str, user_label: str | None = None) -> None:
        self._change(
            actor,
            action=AUDIT_ACTION_SERVICE_ACCESS_REVOKE,
            user_id=user_id,
            group_path=group_path,
            user_label=user_label,
            apply=self._membership.remove_member,
        )

    def _change(
        self,
        actor: AuthContext,
        *,
        action: str,
        user_id: str,
        group_path: str,
        user_label: str | None,
        apply,
    ) -> None:
        with audited(
            self._db,
            actor,
            action,
            target_type="user",
            target_id=user_id,
            target_label=user_label,
            # Hub scope, explicitly. Model access is not an organization's
            # property, and defaulting the scope from the operator's own
            # organization would file the row under a tenant that had nothing
            # to do with it.
            org_id=None,
            detail={"group_path": group_path},
        ):
            # Inside the transaction on purpose: see the module docstring. A
            # raise here rolls the row back with it.
            apply(user_id=user_id, group_path=group_path)
