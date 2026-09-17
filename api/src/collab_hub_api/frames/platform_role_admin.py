"""Granting and revoking the operator role, by a person rather than by a claim.

The other half of :mod:`.platform_role_sync`. That module reconciles rows the
identity provider owns; this one is an operator deciding, from the admin panel.

They must not fight, and ``source`` is what keeps them apart:

* **A grant here is ``manual``**, the same kind of row the documented bootstrap
  insert creates, and sync never touches it. So it outlives the admin group and
  is not removed when the provider stops saying that person is an admin. That
  is a real asymmetry rather than an oversight -- a hand-granted role should
  not evaporate because a group was renamed -- and the panel says so where the
  grant is made, because an admin who does not know it would be surprised in
  the worst possible way.

* **A revoke here reaches any row**, whatever its source. Taking authority away
  must never be refused on the grounds of where it came from. A revoked ``idp``
  row stays ``idp``, so if that person is still in the admin group their next
  sign-in grants it again -- correct, because the group is then still saying
  they are an admin, and the panel says that too rather than letting it look
  like the revoke failed.

Every change writes its audit row in the same transaction, through the same
primitive every other privileged mutation uses.
"""

from __future__ import annotations

from .audit import (
    AUDIT_ACTION_PLATFORM_ROLE_GRANT,
    AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
    audited,
)
from .auth import AuthContext
from .orgs import PLATFORM_ROLE_ACTIVE, PLATFORM_ROLE_OPERATOR, PLATFORM_ROLE_REVOKED
from .platform_role_sync import PLATFORM_ROLE_SOURCE_MANUAL

__all__ = ["PostgresPlatformRoleAdmin"]


class PostgresPlatformRoleAdmin:
    """Operator-role changes made by an administrator."""

    configured = True

    def __init__(self, db) -> None:
        self._db = db

    def grant(self, actor: AuthContext, *, user_id: str, user_label: str | None = None) -> None:
        with audited(
            self._db,
            actor,
            AUDIT_ACTION_PLATFORM_ROLE_GRANT,
            target_type="user",
            target_id=user_id,
            target_label=user_label,
            org_id=None,
            detail={"origin": "admin_panel"},
        ) as event:
            # An upsert, because the common case is re-granting somebody whose
            # role was revoked earlier, and ``user_id`` is the primary key.
            #
            # ``source`` is overwritten to ``manual`` deliberately: a person has
            # now decided this, so the row stops being sync's to remove. The
            # reverse never happens -- sync cannot claim a manual row.
            event.conn.execute(
                """
                INSERT INTO collab_platform_roles (user_id, role, status, source, granted_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                   SET role = EXCLUDED.role,
                       status = EXCLUDED.status,
                       source = EXCLUDED.source,
                       granted_by = EXCLUDED.granted_by,
                       granted_at = now()
                """,
                (
                    user_id,
                    PLATFORM_ROLE_OPERATOR,
                    PLATFORM_ROLE_ACTIVE,
                    PLATFORM_ROLE_SOURCE_MANUAL,
                    actor.user,
                ),
            )

    def revoke(self, actor: AuthContext, *, user_id: str, user_label: str | None = None) -> None:
        with audited(
            self._db,
            actor,
            AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
            target_type="user",
            target_id=user_id,
            target_label=user_label,
            org_id=None,
            detail={"origin": "admin_panel"},
        ) as event:
            # No ``source`` in the WHERE, unlike sync's revoke: an administrator
            # may take away authority the provider granted. The row keeps its
            # source, so a synced one comes back at that person's next sign-in
            # if the group still lists them.
            event.conn.execute(
                """
                UPDATE collab_platform_roles
                   SET status = %s
                 WHERE user_id = %s AND status = %s
                """,
                (PLATFORM_ROLE_REVOKED, user_id, PLATFORM_ROLE_ACTIVE),
            )
