"""Organization membership reads for the auth choke point.

Collab Server owns the organization model; the identity provider only
authenticates. The tables are created by the versioned runner in
:mod:`.collab_schema` (issue #62); this module is the read surface issue #63
needs — "which organization does this subject belong to, and in what role" —
and nothing more.

**Read-only on purpose.** Organization and membership *writes* (create an org,
invite, accept, remove, transfer ownership) are separate issues with their own
policy decisions; adding speculative write methods here would bake in guesses
about them. The store carries exactly the reads the auth choke point performs:
the membership lookup (issue #63) and the platform-role lookup (issue #87).
Platform-role *writes* are likewise absent on purpose — the bootstrap operator
is a documented psql insert, and grant/revoke endpoints are built the second
time they are needed.

Backend semantics mirror ``usage.py``/``history.py``: a relational feature
riding the shared ``frames.postgres`` URL, with ``InMemory`` as a test/dev
override and an unavailable store when no database is configured. The
unavailable store **raises** rather than returning ``None`` — membership is an
authorization input, so "no backend" has to be indistinguishable from a denial
at the call site and can never degrade into "this user has no organization".
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

orgs_logger = logging.getLogger("frames_server.orgs")

MEMBERSHIP_ACTIVE = "active"
MEMBERSHIP_REMOVED = "removed"

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"

PLATFORM_ROLE_OPERATOR = "operator"
"""The only platform-scoped role (issue #87). A **second axis** from the
org-scoped ``role`` above: an org owner has authority inside one organization,
an operator across the deployment. Never merge them."""

PLATFORM_ROLE_ACTIVE = "active"
PLATFORM_ROLE_REVOKED = "revoked"


class OrgsUnavailableError(RuntimeError):
    """Raised when membership is needed but no organization backend exists."""


class OrgSchemaMissingError(OrgsUnavailableError):
    """The database is reachable but has never had the ``collab_`` migrations run.

    Reachable at runtime through one narrow gap in the startup preflight: with
    ``auto_migrate`` off, a pod that starts while Postgres is unreachable cannot
    read the schema version, so it tolerates the unknown and serves (the
    alternative is a crash loop on a transient outage). If the database then
    comes back *and* nobody ever ran the migration out of band, the first
    membership query hits a table that does not exist.

    Left alone that surfaces as ``UndefinedTable`` — a ``ProgrammingError``, not
    one of the "database unavailable" classes — so it would answer 500 at
    request time, which is precisely the failure the preflight exists to
    prevent. Deriving from :class:`OrgsUnavailableError` maps it onto the same
    fail-closed 503 instead: the server genuinely cannot answer the membership
    question, and it must not guess.
    """


@dataclass(frozen=True)
class OrgMembership:
    """One login's home-organization row.

    At most one per ``user_id`` — ``collab_org_members.user_id`` is the primary
    key, so this is an exact primary-key lookup and a second row for the same
    login is impossible by construction.
    """

    user_id: str
    org_id: str
    role: str
    status: str

    @property
    def is_active(self) -> bool:
        """Whether this row grants organization membership right now.

        A ``removed`` row still binds the login to its organization (that is
        what keeps a removed user from re-registering elsewhere) but grants
        nothing, so every authorization decision must ask this, never merely
        whether a row exists.
        """

        return self.status == MEMBERSHIP_ACTIVE


@dataclass(frozen=True)
class ResolvedPrincipal:
    """Everything the auth choke point reads about one login, in one answer.

    Membership and platform role are different authority axes, but they are
    resolved together — one store call, one pooled connection, one round trip
    — because they are consumed together on every authenticated request, and
    because a *split* read has a worse failure mode: a membership that
    resolved followed by a platform-role lookup that failed would be a partial
    authorization answer, indistinguishable downstream from "not an operator".
    Together they either both answer or the whole request fails closed.
    """

    membership: OrgMembership | None
    """The login's home-organization row, removed rows included (the caller
    decides what removal means), or ``None`` if it has none."""

    platform_role: str | None
    """The login's **active** platform role, or ``None``. ``None`` covers "no
    row" and "revoked" alike: unlike a removed membership — which still binds
    the login to its organization — a revoked platform role carries no
    residual semantics, so collapsing it here keeps a revoked operator from
    being one forgotten ``status`` check away from authority."""


class OrgStore(ABC):
    """The organization membership relation, as the auth path needs it."""

    @abstractmethod
    def get_membership(self, user_id: str) -> OrgMembership | None:
        """Return the login's home-organization row, or ``None`` if it has none.

        Removed rows are returned, not filtered out: the caller decides what a
        removed membership means, and the distinction between "removed" and
        "never a member" is real even though both currently resolve to
        ``no_organization``.

        Raises rather than returning ``None`` when the backend is unavailable.
        """

        raise NotImplementedError

    @abstractmethod
    def resolve_principal(self, user_id: str) -> ResolvedPrincipal:
        """Return the login's membership and active platform role together.

        The auth choke point's read: both axes in one call so the backends can
        answer them in one round trip (the Postgres store does), and so the
        answer is never partial.

        Raises rather than degrading when the backend is unavailable, for the
        same reason as :meth:`get_membership`: these are authorization inputs,
        and "cannot tell" must never quietly become "no" — or, for the
        platform axis, quietly become a context missing an operator's
        authority while claiming to be complete.
        """

        raise NotImplementedError


class UnavailableOrgStore(OrgStore):
    """Store used when no shared frames Postgres is configured.

    Every call raises :class:`OrgsUnavailableError`, which the app maps to a
    503. There is no best-effort or permissive path: ``make_app`` refuses to
    start a membership-resolving deployment backed by this store, so reaching
    it at runtime means app state was assembled outside ``make_app``.
    """

    def get_membership(self, user_id: str) -> OrgMembership | None:
        raise OrgsUnavailableError("Organization storage is not configured")

    def resolve_principal(self, user_id: str) -> ResolvedPrincipal:
        raise OrgsUnavailableError("Organization storage is not configured")


class InMemoryOrgStore(OrgStore):
    """Process-local membership, for tests and single-process development.

    Not durable and not shared between replicas; the chart refuses to select it
    for any deployment (``frames.orgs.backend`` has no chart value).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memberships: dict[str, OrgMembership] = {}
        self._platform_roles: dict[str, tuple[str, str]] = {}

    def set_membership(
        self,
        user_id: str,
        org_id: str,
        role: str = ROLE_MEMBER,
        status: str = MEMBERSHIP_ACTIVE,
    ) -> OrgMembership:
        """Seed or replace a membership row (dev/test only; no write API exists yet)."""

        membership = OrgMembership(user_id=user_id, org_id=org_id, role=role, status=status)
        with self._lock:
            self._memberships[user_id] = membership
        return membership

    def get_membership(self, user_id: str) -> OrgMembership | None:
        with self._lock:
            return self._memberships.get(user_id)

    def set_platform_role(
        self,
        user_id: str,
        role: str = PLATFORM_ROLE_OPERATOR,
        status: str = PLATFORM_ROLE_ACTIVE,
    ) -> None:
        """Seed or replace a platform-role row (dev/test only; grants have no API)."""

        with self._lock:
            self._platform_roles[user_id] = (role, status)

    def resolve_principal(self, user_id: str) -> ResolvedPrincipal:
        # Through get_membership on purpose, so a test subclass that counts or
        # fails membership lookups (test_membership_auth.RecordingOrgStore)
        # keeps seeing every read the auth path performs.
        membership = self.get_membership(user_id)
        with self._lock:
            granted = self._platform_roles.get(user_id)
        platform_role = granted[0] if granted is not None and granted[1] == PLATFORM_ROLE_ACTIVE else None
        return ResolvedPrincipal(membership=membership, platform_role=platform_role)


class PostgresOrgStore(OrgStore):
    """Membership reads against ``collab_org_members`` over the shared pool.

    No ``_ensure_schema``: unlike the ``frames_server_`` stores, the ``collab_``
    tables are created by the versioned, lock-guarded runner in
    :mod:`.collab_schema`, invoked once at startup. A store that also emitted
    DDL would reintroduce exactly the unlocked ``CREATE TABLE IF NOT EXISTS``
    race that runner exists to remove.

    Nothing is cached. Per-request resolution is what makes a removal take
    effect on the *next* request; a cache would trade that for a revocation
    delay, and would also mean a Postgres outage denied some callers and served
    others from stale state. Add one only on operational evidence.
    """

    def __init__(self, db):
        self._db = db
        self._reported_missing_schema = False

    def get_membership(self, user_id: str) -> OrgMembership | None:
        import psycopg

        try:
            with self._db.connection() as conn:
                row = conn.execute(
                    "SELECT user_id, org_id, role, status FROM collab_org_members WHERE user_id = %s",
                    (user_id,),
                ).fetchone()
        except psycopg.errors.UndefinedTable as exc:
            raise self._missing_schema() from exc
        if row is None:
            return None
        return OrgMembership(
            user_id=row["user_id"],
            org_id=row["org_id"],
            role=row["role"],
            status=row["status"],
        )

    def resolve_principal(self, user_id: str) -> ResolvedPrincipal:
        import psycopg

        # One connection checkout and one round trip for both axes: this runs
        # on every authenticated request, and a second sequential checkout
        # would double pool pressure at the choke point and let a Postgres
        # blip fail a request *between* the two halves of one authorization
        # answer. Both sides are primary-key probes, so the join costs what
        # the single membership lookup cost.
        #
        # The platform-role status filter lives in the SQL, not in Python: a
        # revoked grant must resolve to "no role" with no code path in between
        # that could forget to check. Membership status is NOT filtered — the
        # caller decides what a removed row means, exactly as get_membership.
        try:
            with self._db.connection() as conn:
                row = conn.execute(
                    """
                    SELECT m.user_id AS member_user_id, m.org_id, m.role, m.status,
                           p.role AS platform_role
                    FROM (VALUES (%s)) AS principal (user_id)
                    LEFT JOIN collab_org_members m ON m.user_id = principal.user_id
                    LEFT JOIN collab_platform_roles p
                           ON p.user_id = principal.user_id AND p.status = %s
                    """,
                    (user_id, PLATFORM_ROLE_ACTIVE),
                ).fetchone()
        except psycopg.errors.UndefinedTable as exc:
            raise self._missing_schema() from exc
        membership = None
        if row is not None and row["member_user_id"] is not None:
            membership = OrgMembership(
                user_id=row["member_user_id"],
                org_id=row["org_id"],
                role=row["role"],
                status=row["status"],
            )
        return ResolvedPrincipal(
            membership=membership,
            platform_role=row["platform_role"] if row is not None else None,
        )

    def _missing_schema(self) -> OrgSchemaMissingError:
        message = (
            "collab_org_members does not exist: this database has never had the collab_ "
            "migrations applied. Apply them out of band and record them in "
            "collab_schema_migrations, or enable frames.postgres.autoMigrate and restart. "
            "Every authenticated request answers 503 until then."
        )
        # Once per store, not once per request: a misconfiguration lasts until
        # someone acts on it, and one line per authenticated request would bury
        # the line that says what to do.
        if not self._reported_missing_schema:
            self._reported_missing_schema = True
            orgs_logger.error("collab_schema_missing", extra={"detail": message})
        return OrgSchemaMissingError(message)
