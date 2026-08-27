"""Versioned, lock-guarded auto-migration for the ``collab_`` tables.

The pre-existing ``frames_server_`` stores each run their own idempotent
``CREATE TABLE IF NOT EXISTS`` DDL at startup, unlocked and with no version
history. ``replicaCount: 1`` has masked two problems with that idiom:

- ``CREATE TABLE IF NOT EXISTS`` is **not** concurrency-safe in Postgres. Two
  replicas starting at the same instant can both pass the existence check and
  then race the catalog insert, so one pod dies at startup with a duplicate-key
  error on ``pg_type``/``pg_class`` (issue #84).
- Create-only DDL has no mechanism for *changing* a table later: an ``ALTER``
  or a backfill has nowhere to live and no way to know whether it already ran.

The ``collab_`` tenancy tables (issue #62) therefore use a single runner rather
than per-store ``_ensure_schema`` methods:

- one transaction takes ``pg_advisory_xact_lock(COLLAB_SCHEMA_LOCK_KEY)``
  **before** any catalog access, so concurrent replica startups serialize; the
  lock is transaction-scoped, so it releases on commit or rollback with no
  unlock bookkeeping and no leak if the pod dies mid-migration;
- an ordered ``collab_schema_migrations`` table records what has been applied,
  so later non-idempotent changes are simply appended as new versions;
- lock, version reads, DDL, and version inserts all commit together — Postgres
  DDL is transactional, so a failed migration leaves nothing half-applied.

The existing ``frames_server_`` stores are deliberately untouched here: issue
#84 tracks converging them, and rewriting five shipped stores is not this
issue's scope. They keep their (masked) startup race.

Wiring follows the house pattern: the tables ride the shared
``frames.postgres.url`` and are migrated at app startup when
``frames.postgres.auto_migrate`` is set, over the shared psycopg pool
(issue #58) — never a fresh ``psycopg.connect``.
"""

from __future__ import annotations

import logging

collab_logger = logging.getLogger("frames_server.collab_schema")

# Fixed advisory-lock key for collab schema migrations:
# int.from_bytes(b"collab_1", "big"). Any 64-bit value would do as long as
# every writer of collab_ DDL uses the same one; deriving it from a name makes
# a collision with another advisory-lock user of the shared database unlikely,
# and makes the constant greppable.
COLLAB_SCHEMA_LOCK_KEY = int.from_bytes(b"collab_1", "big")

# Bookkeeping table recording which migrations this database has applied.
COLLAB_SCHEMA_VERSION_TABLE = "collab_schema_migrations"

NEUTRAL_ORG_NAME = "Unnamed organization"
"""The name every organization starts with, including one created by accepting
an invitation (issue #89).

Neutral by ratified decision — the dated Gate B revision of 2026-08-04
replaced the original "derive the name from login information" resolution.
This constant is the Python-side spelling of migration v1's column default;
:func:`~.invitations.PostgresInvitationService.accept` never supplies a name,
so the default is what applies and there is exactly one place the placeholder
is written. A test pins the two spellings together.
"""

# Ordered, append-only migration history for the collab_ tables.
#
# RULES:
#   1. Versions are append-only and are never renumbered.
#   2. The statements of a RELEASED version are frozen text. A deployment that
#      has recorded version N will never re-run it, so editing version N's SQL
#      forks reality between old and new databases. Fix a released version by
#      appending a new one.
#   3. Statements stay idempotent where the DDL allows it (IF NOT EXISTS), so a
#      database that was hand-patched between releases does not wedge the whole
#      migration transaction.
COLLAB_SCHEMA_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            # Organizations. `id` is an opaque generated value and the public
            # identifier — there is no slug, and it is never derived from a
            # person's or a company's name. `name` is display-only: nullable,
            # non-unique, and defaulted to a neutral placeholder rather than
            # anything inferred from the creator. `created_by` is the OIDC sub.
            """
            CREATE TABLE IF NOT EXISTS collab_orgs (
                id          text PRIMARY KEY,
                name        text DEFAULT 'Unnamed organization',
                created_at  timestamptz NOT NULL DEFAULT now(),
                created_by  text NOT NULL
            )
            """,
            # One home organization per login. The PRIMARY KEY on user_id
            # (= OIDC sub) *is* that invariant, and it makes the hot query —
            # "which home org does this sub belong to?" — an exact PK lookup.
            #
            # `status` is load-bearing: removal keeps the row and flips it to
            # 'removed' so the home-org binding stays enforceable (a removed
            # user cannot re-register into a different org), while the auth
            # choke point resolves such a row to "no organization" (issue #63).
            # Nothing here ever deletes a membership row.
            #
            # `email`/`display_name` are display and contact fields only. They
            # are never ACL principals — authorization keys on `user_id`.
            """
            CREATE TABLE IF NOT EXISTS collab_org_members (
                user_id       text PRIMARY KEY,
                org_id        text NOT NULL REFERENCES collab_orgs(id),
                role          text NOT NULL CHECK (role IN ('owner', 'member')),
                email         text,
                display_name  text,
                created_at    timestamptz NOT NULL DEFAULT now(),
                status        text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'removed'))
            )
            """,
            # Membership listing ("everyone in org X") is the only access path
            # that is not the user_id primary key.
            """
            CREATE INDEX IF NOT EXISTS collab_org_members_org
            ON collab_org_members (org_id)
            """,
        ),
    ),
    # No `collab_invitations` table here on purpose. Nothing in this phase
    # reads or writes invitations, and baking guessed columns into every
    # deployment that runs auto-migrate meanwhile buys nothing: with the
    # versioned runner above, adding that table later costs exactly one
    # appended migration version.
    #
    # `workspace_id` is not modeled anywhere: it is the literal constant
    # "default" and there is no workspaces table.
    (
        2,
        (
            # Platform-scoped roles (issue #87, Gate E). A second authority
            # axis from the org-scoped collab_org_members.role: an org owner
            # has authority inside one organization, an operator across the
            # deployment. Deliberately not merged into the membership table
            # and deliberately not a Keycloak realm role — Keycloak
            # authenticates, this server authorizes.
            #
            # `user_id` is the OIDC sub, like every other principal column.
            # `granted_by` is nullable because the bootstrap row is inserted
            # by hand (recorded as an `operator.manual` audit event — see the
            # runbook in docs/frames-operations.md); there are no grant/revoke
            # endpoints yet, on purpose. The CHECKs guard the actual write
            # path for this beta, which is psql: a typo'd role or status in a
            # hand-run insert is refused by the schema instead of silently
            # granting nothing. `status` defaults to 'active' to keep the
            # documented bootstrap insert a genuine one-liner, mirroring
            # collab_org_members.status.
            """
            CREATE TABLE IF NOT EXISTS collab_platform_roles (
                user_id     text PRIMARY KEY,
                role        text NOT NULL CHECK (role IN ('operator')),
                granted_at  timestamptz NOT NULL DEFAULT now(),
                granted_by  text,
                status      text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked'))
            )
            """,
            # The audit event log (issue #87). Every privileged action commits
            # its row here in the same transaction as the mutation it
            # describes — see frames/audit.py, the only writer.
            #
            # `actor` is the sub (immutable); `actor_label`/`target_label`
            # snapshot the human-readable name AT TIME OF ACTION, because a
            # row read months later showing only a UUID is useless and the
            # live display fields can change or vanish. `org_id` is nullable:
            # operator actions can be hub-scoped. `detail` is redacted by its
            # writers and must NEVER hold an invitation secret.
            #
            # `action` and `target_type` are CHECK-constrained to the ratified
            # closed vocabularies: rows are found by exact-match runbook
            # queries, so an unvocabularied value is a silent audit gap, and
            # the constraint also covers rows written from psql — which
            # includes every `operator.manual` entry. The lists here and in
            # frames/audit.py (AUDIT_ACTIONS / AUDIT_TARGET_TYPES) are pinned
            # together by a unit test; adding an action later is an appended
            # migration that replaces the constraint, plus the AUDIT_ACTIONS
            # change, on the issue that introduces the action.
            #
            # Append-only is a code convention, not an enforced boundary: the
            # application role owns this table (auto-migration creates it over
            # the runtime pool), so a REVOKE would be theater — an owner can
            # re-grant to itself. The enforceable claim, tested against the
            # code, is that no application code path updates or deletes rows.
            #
            # No indexes beyond the primary key, deliberately: the log is read
            # with psql per the runbook and beta volume is hundreds of rows.
            """
            CREATE TABLE IF NOT EXISTS collab_audit_events (
                id            bigserial PRIMARY KEY,
                at            timestamptz NOT NULL DEFAULT now(),
                actor         text NOT NULL,
                actor_label   text,
                action        text NOT NULL
                              CHECK (action IN ('invitation.send', 'invitation.redeem',
                                                'invitation.revoke', 'membership.create',
                                                'org.create', 'org.rename', 'operator.manual')),
                target_type   text
                              CHECK (target_type IN ('org', 'user', 'invitation')),
                target_id     text,
                target_label  text,
                org_id        text,
                detail        jsonb
            )
            """,
        ),
    ),
    (
        3,
        (
            # Invitations (issue #89, Gate B). Version 2 has shipped, so this
            # is an appended version and never an amendment of it — the rule
            # stated at the top of this list.
            #
            # `id` is an opaque generated value (uuid4 hex): it is quoted in
            # error messages, audit rows, and the SES `invitation_id` tag, so
            # it must carry nothing about the invitee. The **secret** is not
            # here in any form — `token_hash` is the SHA-256 hex of a 256-bit
            # random secret, and the raw secret exists only in memory between
            # minting and handing it to the email adapter. UNIQUE both
            # enforces single-issuance-per-secret and makes redemption an
            # index lookup.
            #
            # `org_id` NULL means an org-creating invitation: acceptance mints
            # a new organization with the accepter as its owner. A non-NULL
            # value is a join-this-org invitation and is immutable for the
            # row's whole life — the authorization checks on revoke read it,
            # and a mutable target would make those checks racy.
            #
            # `email` is the invited address **exactly as issued** (Gate B
            # chose exact match, 2026-08-03: there is no canonicalization
            # ruleset, so there is no canonical column and no second spelling
            # to keep in sync). Acceptance compares the verified `email`
            # claim to this string with `=`.
            #
            # `status` is the stored state only. `expired` is DERIVED
            # (`pending` past `expires_at`) so no sweeper has to keep a fourth
            # state true; see frames/invitations.py effective_status.
            #
            # The `accepted_*` columns are the replay record: they make a
            # redeemed token idempotent for the login that redeemed it, and
            # `accepted_org_id` records which organization the acceptance
            # produced (the created one, for a NULL-`org_id` invitation).
            """
            CREATE TABLE IF NOT EXISTS collab_invitations (
                id               text PRIMARY KEY,
                org_id           text REFERENCES collab_orgs(id),
                email            text NOT NULL,
                token_hash       text NOT NULL UNIQUE,
                status           text NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'accepted', 'revoked')),
                created_at       timestamptz NOT NULL DEFAULT now(),
                created_by       text NOT NULL,
                expires_at       timestamptz NOT NULL,
                accepted_at      timestamptz,
                accepted_by      text,
                accepted_org_id  text REFERENCES collab_orgs(id),
                revoked_at       timestamptz,
                revoked_by       text
            )
            """,
            # "Every invitation for org X, newest first" is the owner's list
            # view and the only access path that is neither the primary key
            # nor the unique token hash. The operator's list is unfiltered and
            # sorts on created_at, which this index does not serve — beta
            # volume is hundreds of rows, so that sort stays a plain one.
            """
            CREATE INDEX IF NOT EXISTS collab_invitations_org
            ON collab_invitations (org_id, created_at DESC)
            """,
            # `org_id` immutability, enforced rather than assumed.
            #
            # Acceptance reads the invitation once outside its transaction to
            # decide the audited action and scope — `org.create` for a
            # NULL-`org_id` invitation, `invitation.redeem` otherwise — because
            # audited() fixes both before the body runs and refuses to let the
            # body rewrite them. It then re-reads FOR UPDATE and re-checks
            # everything. That design is safe only because `org_id` cannot
            # change between the two reads: if it could, an acceptance could
            # commit a join to an existing organization while its audit row
            # said `org.create` for an organization that was never created —
            # a log that describes something that did not happen, which is the
            # one failure the whole audited primitive exists to prevent.
            #
            # No application path updates this column, but "no code does it"
            # is not the same claim as "it cannot happen": the beta's write
            # path for corrections is psql, by hand, and this table's rows are
            # exactly the sort a person edits during an incident. The trigger
            # covers that case too, which is the point of putting it in the
            # database rather than in a comment.
            #
            # check_violation (23514) so it arrives as psycopg.errors.
            # CheckViolation — the same class an inline CHECK would raise, had
            # one been able to see the old row.
            """
            CREATE OR REPLACE FUNCTION collab_invitations_freeze_org_id()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.org_id IS DISTINCT FROM OLD.org_id THEN
                    RAISE EXCEPTION
                        'collab_invitations.org_id is immutable (invitation %)', OLD.id
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """,
            # DROP-then-CREATE rather than CREATE OR REPLACE TRIGGER, which is
            # PostgreSQL 14+; the migration runner's idempotence rule should
            # not quietly raise this chart's minimum server version.
            """
            DROP TRIGGER IF EXISTS collab_invitations_org_id_immutable ON collab_invitations
            """,
            """
            CREATE TRIGGER collab_invitations_org_id_immutable
            BEFORE UPDATE ON collab_invitations
            FOR EACH ROW EXECUTE FUNCTION collab_invitations_freeze_org_id()
            """,
        ),
    ),
    (
        4,
        (
            # Account provisioning, keyed by the **folded** address (#172).
            #
            # This table is two things at once, and the second is why it is a
            # table rather than a column on `collab_invitations`:
            #
            # 1. the durable phase of setting an invitee's account up, so an
            #    interrupted run can be resumed rather than guessed at;
            # 2. **the claim that serializes provisioning.** The primary key is
            #    the folded address, so the first writer wins and a second
            #    concurrent request's insert conflicts. It learns it is not the
            #    creator and reads this row instead of creating a second
            #    account.
            #
            # `pg_advisory_xact_lock` cannot do that job here. Invitation rows
            # commit *before* the external calls (the audited claim is the
            # recoverable partial state), and a transaction-scoped lock is gone
            # at commit -- so the window this table closes is exactly the window
            # that lock leaves open.
            #
            # Keyed per **address**, not per invitation, because two live
            # invitations for one address are deliberate on the `/v1` routes and
            # pinned by a regression test. Provisioning happens at most once per
            # address; issuance may repeat. Keying it per invitation would make
            # two invitations own one account.
            #
            # The key is the folded form from `ascii_folded_bytes` (#157), the
            # same one Gate B matches on. Storing the raw address would let
            # `Alice@` and `alice@` each provision an account when only one of
            # them can ever accept.
            """
            CREATE TABLE IF NOT EXISTS collab_provisioned_accounts (
                email_folded     text PRIMARY KEY,
                email            text NOT NULL,
                phase            text NOT NULL
                                 CHECK (phase IN ('claimed', 'account_created',
                                                  'setup_sent', 'complete')),
                idp_user_id      text,
                first_invitation text NOT NULL REFERENCES collab_invitations(id),
                created_at       timestamptz NOT NULL DEFAULT now(),
                updated_at       timestamptz NOT NULL DEFAULT now(),
                -- A phase past `claimed` without an account id is not a state
                -- this workflow can be in, and it is the shape a half-written
                -- recovery would produce. Refused here rather than tolerated,
                -- because the recovery path reads this column to decide whether
                -- to adopt or create.
                CONSTRAINT collab_provisioned_accounts_id_present
                    CHECK (phase = 'claimed' OR idp_user_id IS NOT NULL)
            )
            """,
            # Cleanup and alerting both ask "what has been sitting unfinished",
            # which is a scan by phase over age.
            """
            CREATE INDEX IF NOT EXISTS collab_provisioned_accounts_phase_idx
            ON collab_provisioned_accounts (phase, updated_at)
            """,
        ),
    ),
    (
        5,
        (
            # Widen the audit action vocabulary by one: `service_access.grant`
            # (issue #180). The vocabulary is a CHECK constraint written by
            # version 2, whose text is frozen -- so the constraint is
            # *replaced* here rather than edited there, which is rule 2 of this
            # list applied to a constraint instead of a table.
            #
            # The DROP names the constraint PostgreSQL generated for version
            # 2's unnamed column CHECK, and the ADD gives the replacement that
            # same name -- so this migration is the last one that has to know
            # the generated spelling, and every future widening drops a name
            # this file chose. `IF EXISTS` keeps the statement safe on a
            # database where the constraint was already replaced by hand.
            #
            # Two things make a silently-wrong outcome impossible rather than
            # unlikely: the constraint is NOT VALID-free (it validates the
            # existing rows as it is added, and every existing row holds a
            # value that is still in the set), and a live test inserts one row
            # per action in `AUDIT_ACTIONS` plus one bogus action, so a
            # constraint left behind under a different name -- which would
            # keep refusing the new value -- fails that test rather than
            # production's first grant.
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_action_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_action_check
            CHECK (action IN ('invitation.send', 'invitation.redeem',
                              'invitation.revoke', 'membership.create',
                              'org.create', 'org.rename', 'operator.manual',
                              'service_access.grant'))
            """,
        ),
    ),
    (
        6,
        (
            # What an acceptance owes somebody, durably (issue #180).
            #
            # The audit table records what happened; this records what is still
            # owed, and the two are not interchangeable. The `pending` row is
            # written INSIDE the acceptance's audited transaction, so it lands
            # if and only if the acceptance does -- which is what closes the
            # window between the acceptance committing and the identity-provider
            # call being recorded. Every crash point then resolves to `pending`,
            # and retrying is safe because adding a user to a group is
            # idempotent.
            #
            # PRIMARY KEY (user_id, group_path) because that pair IS the grant.
            # A second invitation to the same person does not create a second
            # thing to do, and `ON CONFLICT DO NOTHING` on the insert keeps the
            # first invitation as the recorded context rather than churning it.
            #
            # `invitation` references collab_invitations for the same reason
            # v4's claim does: the row is only meaningful as a consequence of an
            # invitation, and a dangling id would make the reconciliation list
            # unjoinable to the person it is about.
            """
            CREATE TABLE IF NOT EXISTS collab_service_access_grants (
                user_id    text NOT NULL,
                group_path text NOT NULL,
                state      text NOT NULL
                           CHECK (state IN ('pending', 'granted', 'failed')),
                invitation text NOT NULL REFERENCES collab_invitations(id),
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, group_path)
            )
            """,
            # The reconciliation read is "what is owed, oldest first", which is
            # a scan by state over age. Same shape as v4's index and the same
            # reason: this is the query a sweep and an operator page both run.
            """
            CREATE INDEX IF NOT EXISTS collab_service_access_grants_state_idx
            ON collab_service_access_grants (state, created_at)
            """,
        ),
    ),
)


def _validate_migrations() -> None:
    """Fail loudly at import if the migration list breaks its own rules.

    Cheap insurance for the append-only invariant: a duplicated or out-of-order
    version would otherwise be found by a production database applying the
    wrong thing (or nothing).
    """

    versions = [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    if versions != sorted(set(versions)) or (versions and versions[0] != 1):
        raise RuntimeError(f"COLLAB_SCHEMA_MIGRATIONS must have unique, ascending versions starting at 1: {versions}")


_validate_migrations()

LATEST_COLLAB_SCHEMA_VERSION = COLLAB_SCHEMA_MIGRATIONS[-1][0] if COLLAB_SCHEMA_MIGRATIONS else 0
"""Highest migration version this build knows how to apply."""


def run_collab_schema_migrations(db) -> None:
    """Apply any unapplied ``collab_`` migrations, safely under concurrency.

    ``db`` is a pooled :class:`~collab_hub_api.frames.db.PostgresDatabase`;
    ``db.connection()`` yields psycopg_pool's transaction-scoped context
    manager, so the advisory lock, the version bookkeeping, and the DDL all
    commit (or roll back) together.

    Concurrent replicas serialize on the advisory lock; whichever gets it
    second re-reads the version table *inside* the lock, finds the versions
    already recorded, and applies nothing. Failure semantics match the existing
    stores' ``auto_migrate``: an unreachable database raises here and the pod
    fails to start rather than serving against a schema it cannot verify.
    """

    with db.connection() as conn:
        # Serialize before touching the catalog at all — including the version
        # table's own CREATE, which has exactly the same race as any other
        # CREATE TABLE IF NOT EXISTS.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (COLLAB_SCHEMA_LOCK_KEY,))
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {COLLAB_SCHEMA_VERSION_TABLE} (
                version     integer PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        row = conn.execute(f"SELECT COALESCE(MAX(version), 0) AS version FROM {COLLAB_SCHEMA_VERSION_TABLE}").fetchone()
        applied = row["version"] if row else 0
        for version, statements in COLLAB_SCHEMA_MIGRATIONS:
            if version <= applied:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {COLLAB_SCHEMA_VERSION_TABLE} (version) VALUES (%s)",
                (version,),
            )


def applied_collab_schema_version(db) -> int:
    """Return the highest ``collab_`` migration version this database has applied.

    ``0`` means nothing has been applied — including the case where the
    bookkeeping table does not exist yet. Intended for operational checks and
    tests; the runner reads the version itself, inside its lock.
    """

    import psycopg

    try:
        with db.connection() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(version), 0) AS version FROM {COLLAB_SCHEMA_VERSION_TABLE}"
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        return 0
    return row["version"] if row else 0


class CollabSchemaVersionError(RuntimeError):
    """The database's ``collab_`` schema is older than this build requires."""


def check_collab_schema_version(db, *, auto_migrate: bool) -> int | None:
    """Verify at startup that the ``collab_`` schema is new enough to serve.

    Called once from ``make_app``, and only when something actually reads these
    tables (issue #96: the check has no meaning on a deployment that does not).
    Three cases, decided deliberately:

    * **Behind** (``applied < LATEST_COLLAB_SCHEMA_VERSION``) — raises. This is
      the case the check exists for: with ``auto_migrate`` off and the
      migration never run out of band, the pod would otherwise start cleanly
      and fail later with ``relation "collab_org_members" does not exist``, at
      request time, in whichever endpoint happened to touch it first. An
      operator who has to run a migration should be told that, at startup,
      instead of reading a traceback out of a request log. (With
      ``auto_migrate`` on this is unreachable in the ordinary case — the
      migration has just run — so reaching it means the migration silently did
      not apply, which is also worth refusing to serve.)

    * **Ahead** (``applied > LATEST_COLLAB_SCHEMA_VERSION``) — logs, and is
      **not** fatal. A newer replica migrating the shared database while older
      replicas are still running is what an ordinary rolling update looks like;
      making that fatal would turn every deploy into an outage of the old
      replicas and would block rollbacks entirely. Migrations here are
      append-only, so an older build's statements remain valid against a newer
      schema. This is a deliberate decision, not an omission.

    * **Unreachable** — logs, and is **not** fatal *here*. Nothing is asserted
      about the schema in this case: the check simply could not run, and the
      version is reported as ``None``.

      Read that narrowly. It does **not** mean a membership deployment always
      survives a startup outage. With ``auto_migrate`` on,
      :func:`run_collab_schema_migrations` has already run — and already raised
      — before this function is called, so the pod fails to start and retries.
      That is deliberate and predates this check: a pod that skipped its
      migration and started anyway would come up against tables that do not
      exist, and would keep serving errors after the database returned, because
      migrations only run at startup. Crash-looping until Postgres is reachable,
      then migrating, is the correct outcome for that deployment.

      What this branch actually buys is the ``auto_migrate`` **off** case, where
      the pod touches no schema at startup and a reachable-database requirement
      would be a new dependency rather than an existing one.

    Returns the applied version, or ``None`` when the database could not be
    reached.
    """

    try:
        from .db import postgres_error_classes

        database_errors = postgres_error_classes()
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        database_errors = (Exception,)

    try:
        applied = applied_collab_schema_version(db)
    except database_errors as exc:
        collab_logger.warning(
            "collab_schema_version_check_skipped",
            extra={"reason": type(exc).__name__, "required": LATEST_COLLAB_SCHEMA_VERSION},
        )
        return None

    if applied < LATEST_COLLAB_SCHEMA_VERSION:
        raise CollabSchemaVersionError(
            f"The collab_ schema in this database is at version {applied}, but this build "
            f"requires version {LATEST_COLLAB_SCHEMA_VERSION}. "
            + (
                "Auto-migration is enabled, so the migration ran and did not take effect — "
                "check the startup logs for the failure."
                if auto_migrate
                else "Auto-migration is disabled "
                "(COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE / frames.postgres.autoMigrate): "
                "apply the outstanding collab_ migrations out of band and record them in "
                f"{COLLAB_SCHEMA_VERSION_TABLE}, or enable auto-migration."
            )
        )

    if applied > LATEST_COLLAB_SCHEMA_VERSION:
        # Normal and transient mid-rollout; see the docstring.
        collab_logger.warning(
            "collab_schema_version_ahead",
            extra={"applied": applied, "required": LATEST_COLLAB_SCHEMA_VERSION},
        )
    return applied
