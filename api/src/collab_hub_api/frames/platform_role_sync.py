"""Who is an operator, decided by the identity provider and copied here.

Until now the only way to grant the operator role was a hand-written ``psql``
insert, recorded by an ``operator.manual`` audit row. That is the right
bootstrap and the wrong steady state: it puts the grant outside the system an
organization actually administers identity in, and it does not scale past the
first person.

So the identity provider owns the fact and this module keeps a copy. Keycloak
is used as a plain OIDC provider here -- nothing in this module calls its admin
API. At sign-in the verified ID token's ``groups`` claim is read and
``collab_platform_roles`` is reconciled against it.

Why a copy at all, rather than reading the claim on every request
----------------------------------------------------------------
Because authorization is resolved from this server's own stores on the request
being authorized (see :mod:`..web.authz`), which is what makes the stateless
session cookie acceptable. A claim read per request would work on the bearer
axis and not on the browser one, where the session deliberately carries
identity and nothing else. One source for both axes is worth more than the
freshness a second mechanism would buy.

The cost is stated rather than hidden: on the browser axis a revocation takes
effect at the holder's next sign-in, not their next request. Before this
module, revoking a row locked its holder out immediately. That is a real
weakening, and it is the price of the identity provider owning the fact.

``source``, and why a column was worth a migration
--------------------------------------------------
Sync must be able to *remove*. A copy that only ever adds is worse than no copy
at all: dropping someone from the admin group would leave their row standing
forever, and the deployment would believe the identity provider had revoked
them. So reconcile revokes as well as grants.

Which immediately endangers the bootstrap operator, whose row was inserted by
hand and whose subject may be in no group at all. If sync could revoke that
row, the first sign-in after this ships would lock the first admin out of the
deployment they just bootstrapped.

Hence ``source``: ``manual`` rows are administered here and never touched by
sync; ``idp`` rows are sync's own and it may grant or revoke them freely. It is
the split any OIDC group sync ends up needing, and for this reason.
"""

from __future__ import annotations

from collections.abc import Sequence

from .audit import (
    AUDIT_ACTION_PLATFORM_ROLE_GRANT,
    AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
    audited,
)
from .auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from .orgs import PLATFORM_ROLE_ACTIVE, PLATFORM_ROLE_OPERATOR, PLATFORM_ROLE_REVOKED

__all__ = [
    "PLATFORM_ROLE_SOURCE_IDP",
    "PLATFORM_ROLE_SOURCE_MANUAL",
    "DisabledPlatformRoleSync",
    "InMemoryPlatformRoleSync",
    "PostgresPlatformRoleSync",
]

PLATFORM_ROLE_SOURCE_MANUAL = "manual"
"""Administered by hand (the documented bootstrap insert). Sync never touches these."""

PLATFORM_ROLE_SOURCE_IDP = "idp"
"""Created by this module from a groups claim. Sync owns these rows outright."""


class DisabledPlatformRoleSync:
    """What a deployment that has named no admin group does: nothing.

    The default. A deployment with no ``admin_group`` configured behaves
    exactly as it did before this module existed -- roles come from the table
    and only from the table -- rather than having an empty group name silently
    match nothing and revoke every synced row on first sign-in.
    """

    configured = False

    def reconcile(
        self,
        *,
        user_id: str,
        claim_groups: Sequence[str],
        display: DisplayIdentity,
    ) -> str | None:
        return None


class _PlatformRoleSync:
    """The decision, once, for both backends.

    Only the three storage operations differ between Postgres and memory, so
    only those are left abstract. The rule about what a claim means -- and in
    particular the rule that ``manual`` rows are untouchable in both directions
    -- lives here and nowhere else: a second copy of it is how one backend ends
    up revoking the bootstrap operator that the other protects.
    """

    configured = True

    def __init__(self, *, admin_group: str) -> None:
        self._admin_group = admin_group

    def reconcile(
        self,
        *,
        user_id: str,
        claim_groups: Sequence[str],
        display: DisplayIdentity,
    ) -> str | None:
        """Make this subject's role row match their claim; return the active role.

        Returns the role the subject holds *after* reconciling, or ``None``.
        The return value is the answer the sign-in path wants, so that the
        caller never has to read the row back and never has to decide what a
        revoked row means.
        """

        in_admin_group = self._admin_group in claim_groups
        current = self._current_row(user_id)

        if in_admin_group and _is_sync_owned(current) and not _is_active(current):
            self._grant(user_id=user_id, display=display)
            return PLATFORM_ROLE_OPERATOR

        if not in_admin_group and current is not None and _is_sync_owned(current) and _is_active(current):
            self._revoke(user_id=user_id, display=display)
            return None

        return current["role"] if _is_active(current) else None

    def _current_row(self, user_id: str):
        raise NotImplementedError

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        raise NotImplementedError

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> None:
        raise NotImplementedError


class InMemoryPlatformRoleSync(_PlatformRoleSync):
    """The memory-backed pairing, for dev and for deployments without Postgres.

    Records no audit rows, because ``audited()`` writes to Postgres and there
    is no in-memory audit table to write to. That is the existing shape of
    every memory-backed store in this package rather than a gap introduced
    here, and a deployment that needs the record runs Postgres.
    """

    def __init__(self, org_store, *, admin_group: str) -> None:
        super().__init__(admin_group=admin_group)
        self._store = org_store

    def _current_row(self, user_id: str):
        return self._store.get_platform_role_row(user_id)

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        self._store.set_platform_role(
            user_id,
            PLATFORM_ROLE_OPERATOR,
            PLATFORM_ROLE_ACTIVE,
            PLATFORM_ROLE_SOURCE_IDP,
        )

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> None:
        self._store.set_platform_role(
            user_id,
            PLATFORM_ROLE_OPERATOR,
            PLATFORM_ROLE_REVOKED,
            PLATFORM_ROLE_SOURCE_IDP,
        )


class PostgresPlatformRoleSync(_PlatformRoleSync):
    """The durable pairing: the row and its audit entry in one transaction."""

    def __init__(self, db, *, admin_group: str) -> None:
        super().__init__(admin_group=admin_group)
        self._db = db

    def _current_row(self, user_id: str):
        """This subject's role row as it stands, or ``None``.

        Read on its own connection, outside the audited transaction, because
        the audited primitive always writes a row and most sign-ins change
        nothing: opening it to discover there is nothing to record would make
        every sign-in of every operator a fresh audit entry, and a log that is
        mostly "still an operator" is a log nobody reads.
        """

        with self._db.connection() as conn:
            return conn.execute(
                "SELECT role, status, source FROM collab_platform_roles WHERE user_id = %s",
                (user_id,),
            ).fetchone()

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        with audited(
            self._db,
            _actor(user_id, display),
            AUDIT_ACTION_PLATFORM_ROLE_GRANT,
            target_type="user",
            target_id=user_id,
            target_label=display.email or display.name,
            org_id=None,
            detail={"origin": "oidc", "group": self._admin_group},
        ) as event:
            # An upsert, not an insert: a grant is just as often the *return*
            # of someone whose synced row was revoked when they left the group,
            # and ``user_id`` is the primary key, so a plain insert would fail
            # on exactly the second-most-common case this path serves.
            #
            # The ``WHERE`` on the update arm is the same guard the revoke
            # carries, for the same reason: the row was read on a different
            # connection, so a hand-run grant or revocation landing in between
            # must survive a decision taken against the row as it used to be.
            event.conn.execute(
                """
                INSERT INTO collab_platform_roles (user_id, role, status, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                   SET role = EXCLUDED.role,
                       status = EXCLUDED.status,
                       granted_at = now()
                 WHERE collab_platform_roles.source = %s
                """,
                (
                    user_id,
                    PLATFORM_ROLE_OPERATOR,
                    PLATFORM_ROLE_ACTIVE,
                    PLATFORM_ROLE_SOURCE_IDP,
                    PLATFORM_ROLE_SOURCE_IDP,
                ),
            )

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> None:
        with audited(
            self._db,
            _actor(user_id, display),
            AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
            target_type="user",
            target_id=user_id,
            target_label=display.email or display.name,
            org_id=None,
            detail={"origin": "oidc", "group": self._admin_group},
        ) as event:
            # ``source`` is in the WHERE, not only in the decision above: the
            # row is read on a different connection from the one that writes,
            # so a hand-run grant landing in between must not be overwritten by
            # a decision taken against the row as it used to be.
            event.conn.execute(
                """
                UPDATE collab_platform_roles
                   SET status = %s
                 WHERE user_id = %s AND source = %s AND status = %s
                """,
                (
                    PLATFORM_ROLE_REVOKED,
                    user_id,
                    PLATFORM_ROLE_SOURCE_IDP,
                    PLATFORM_ROLE_ACTIVE,
                ),
            )


def _actor(user_id: str, display: DisplayIdentity) -> AuthContext:
    """The audit row's actor: the subject whose sign-in caused the change.

    Not an invented service principal. Nothing here acts on its own initiative
    -- a person signed in, and their identity provider said what they are -- so
    the honest actor is that person, with the provider recorded in ``detail``.

    ``home_org_id=None`` is hub scope and fails closed: every org-scoped read
    of this context raises rather than silently acting inside whichever
    organization the subject happens to belong to.
    """

    return AuthContext(
        user=user_id,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=display,
        org_role=None,
        platform_role=None,
    )


def _is_active(row) -> bool:
    """Whether a role row (or its absence) currently grants anything."""

    return row is not None and row["status"] == PLATFORM_ROLE_ACTIVE


def _is_sync_owned(row) -> bool:
    """Whether this row is sync's to grant or revoke.

    A missing row counts: the first synced grant has nothing to own yet. A
    ``manual`` row never counts, in either direction -- which is what keeps the
    bootstrap operator signed in, and equally what keeps a deliberate hand
    revocation from being undone by group membership.
    """

    return row is None or row["source"] == PLATFORM_ROLE_SOURCE_IDP
