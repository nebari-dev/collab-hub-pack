"""Coverage for the versioned, lock-guarded ``collab_`` auto-migration (issue #62).

Two layers, because CI has no Postgres service (see ``docs/frames-operations.md``):

- **Always-on unit coverage** against a fake database that models the two
  things the runner depends on for correctness — an advisory lock that really
  serializes threads, and a version table that really remembers. It refuses any
  catalog statement issued without the lock held, so a runner that dropped the
  lock, or took it after the first ``CREATE``, fails here rather than in
  production. Concurrency is exercised with real threads.
- **Opt-in integration coverage** against a live server, skipped unless
  ``COLLAB_HUB_TEST_POSTGRES_URL`` is set. That is where the DDL is proven to be
  valid Postgres, the constraints/index are read back out of the catalog, the
  migration is proven idempotent across restarts, and genuinely concurrent
  migrators are shown not to race.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

psycopg = pytest.importorskip("psycopg")

from collab_hub_api.config import (  # noqa: E402
    Config,
    build_postgres_pools,
    migrate_collab_schema,
    preflight_collab_schema,
)
from collab_hub_api.frames import collab_schema  # noqa: E402
from collab_hub_api.frames.collab_schema import (  # noqa: E402
    COLLAB_SCHEMA_LOCK_KEY,
    COLLAB_SCHEMA_MIGRATIONS,
    LATEST_COLLAB_SCHEMA_VERSION,
    CollabSchemaVersionError,
    applied_collab_schema_version,
    check_collab_schema_version,
    run_collab_schema_migrations,
)

COLLAB_TABLES = (
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_orgs",
    "collab_org_members",
    "collab_platform_roles",
    "collab_audit_events",
    "collab_schema_migrations",
)


def normalize(sql: str) -> str:
    return " ".join(str(sql).split())


class FakeServer:
    """A database that enforces the invariant the runner exists to keep.

    Statements are only accepted from a connection that currently holds the
    advisory lock, and the lock is a real ``threading.Lock``, so concurrent
    migrators genuinely serialize (or genuinely fail).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.applied: list[int] = []
        self.statements: list[str] = []
        self.holder: int | None = None
        self._guard = threading.Lock()

    def record(self, statement: str) -> None:
        with self._guard:
            if self.holder != threading.get_ident():
                raise AssertionError(f"statement issued without holding the collab advisory lock: {statement}")
            self.statements.append(statement)

    def ddl_for(self, table: str) -> list[str]:
        return [sql for sql in self.statements if f"CREATE TABLE IF NOT EXISTS {table} " in sql]


class FakeConnection:
    def __init__(self, server: FakeServer):
        self.server = server
        self._pending: dict | None = None
        self._locked = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        # Transaction end: pg_advisory_xact_lock releases on commit/rollback.
        if self._locked:
            self.server.holder = None
            self.server.lock.release()
            self._locked = False
        return False

    def execute(self, sql, params=None):
        statement = normalize(sql)
        if "pg_advisory_xact_lock" in statement:
            assert params == (COLLAB_SCHEMA_LOCK_KEY,)
            self.server.lock.acquire()
            self._locked = True
            self.server.holder = threading.get_ident()
            self._pending = None
            return self
        self.server.record(statement)
        if statement.startswith("SELECT COALESCE(MAX(version)"):
            self._pending = {"version": max(self.server.applied, default=0)}
            return self
        if statement.startswith("INSERT INTO collab_schema_migrations"):
            (version,) = params
            if version in self.server.applied:
                raise AssertionError(f"migration version {version} applied twice")
            self.server.applied.append(version)
            self._pending = None
            return self
        # Widen the window a concurrent migrator would have to lose.
        time.sleep(0.005)
        self._pending = None
        return self

    def fetchone(self):
        return self._pending


class FakeDatabase:
    database_url = "postgresql://stub/example"

    def __init__(self, server: FakeServer):
        self.server = server

    def connection(self, timeout=None):
        return FakeConnection(self.server)


def test_migration_takes_the_advisory_lock_before_any_catalog_statement():
    server = FakeServer()

    run_collab_schema_migrations(FakeDatabase(server))

    # FakeServer refuses statements issued without the lock, so reaching here
    # already proves ordering; assert the version table's own CREATE is inside
    # it too, since that statement has the same race as any other.
    assert server.statements
    assert server.statements[0].startswith("CREATE TABLE IF NOT EXISTS collab_schema_migrations")


def test_migration_creates_the_org_and_membership_schema():
    server = FakeServer()

    run_collab_schema_migrations(FakeDatabase(server))

    (orgs,) = server.ddl_for("collab_orgs")
    assert "id text PRIMARY KEY" in orgs
    # Display-only: nullable, non-unique, neutral placeholder default.
    assert "name text DEFAULT 'Unnamed organization'" in orgs
    assert "UNIQUE" not in orgs
    assert "created_at timestamptz NOT NULL DEFAULT now()" in orgs
    assert "created_by text NOT NULL" in orgs
    # An opaque id, not a slug.
    assert "slug" not in orgs

    (members,) = server.ddl_for("collab_org_members")
    # One home org per login: the PK on the sub *is* the invariant.
    assert "user_id text PRIMARY KEY" in members
    assert "org_id text NOT NULL REFERENCES collab_orgs(id)" in members
    assert "role text NOT NULL CHECK (role IN ('owner', 'member'))" in members
    assert "status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed'))" in members
    assert "email text," in members and "display_name text," in members

    index_ddl = "CREATE INDEX IF NOT EXISTS collab_org_members_org ON collab_org_members (org_id)"
    assert any(index_ddl in sql for sql in server.statements)


def test_migration_creates_the_operator_and_audit_schema():
    server = FakeServer()

    run_collab_schema_migrations(FakeDatabase(server))

    (roles,) = server.ddl_for("collab_platform_roles")
    # Keyed by the sub, like every principal column.
    assert "user_id text PRIMARY KEY" in roles
    # 'operator' is the only platform role; psql is the write path for this
    # beta, so the CHECKs are what refuse a typo'd hand-run grant.
    assert "role text NOT NULL CHECK (role IN ('operator'))" in roles
    assert "granted_at timestamptz NOT NULL DEFAULT now()" in roles
    # Nullable: the bootstrap row has no granting operator.
    assert "granted_by text," in roles
    assert "status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked'))" in roles

    (events,) = server.ddl_for("collab_audit_events")
    assert "id bigserial PRIMARY KEY" in events
    assert "at timestamptz NOT NULL DEFAULT now()" in events
    # The actor is the immutable sub and is required; the label columns are
    # nullable point-in-time snapshots.
    assert "actor text NOT NULL" in events
    assert "actor_label text," in events
    assert "target_label text," in events
    # The ratified closed vocabularies are CHECK constraints, so they hold
    # even for rows written from psql (operator.manual included). The exact
    # code<->schema list equality is pinned by test_operator_foundation.
    assert (
        "action text NOT NULL CHECK (action IN ('invitation.send', 'invitation.redeem',"
        " 'invitation.revoke', 'membership.create', 'org.create', 'org.rename', 'operator.manual'))" in events
    )
    # Nullable target and org: hub-scoped operator events belong to no org.
    assert "target_type text CHECK (target_type IN ('org', 'user', 'invitation'))" in events
    assert "target_id text," in events
    assert "org_id text," in events
    assert "detail jsonb" in events
    # Append-only is a convention: nothing here (no trigger, no rule) claims
    # to enforce it, because the application role owns the table and any
    # in-schema enforcement would be theater.
    assert "TRIGGER" not in events and "RULE" not in events


def test_no_workspaces_table_is_created_and_invitations_arrived_only_in_v3():
    server = FakeServer()

    run_collab_schema_migrations(FakeDatabase(server))

    created = " ".join(server.statements)
    # ``workspace_id`` is the literal constant "default"; there is no table.
    assert "collab_workspaces" not in created
    assert "workspace_id" not in created
    # Invitations were deliberately absent from versions 1 and 2 (guessed
    # columns in every auto-migrating deployment buy nothing) and were added
    # by issue #89 as an appended version, exactly as version 1's comment
    # anticipated. Their arrival must not have disturbed the earlier ones.
    early = " ".join(
        statement for version, statements in COLLAB_SCHEMA_MIGRATIONS if version <= 2 for statement in statements
    )
    assert "collab_invitations" not in early
    assert "collab_invitations" in created


def test_rerunning_the_migration_applies_nothing():
    server = FakeServer()
    database = FakeDatabase(server)

    run_collab_schema_migrations(database)
    first_pass = list(server.statements)
    assert server.applied == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]

    server.statements.clear()
    run_collab_schema_migrations(database)

    # Second pass touches the version table only: no DDL, no duplicate insert
    # (FakeServer raises on one), and the applied set is unchanged.
    assert all("collab_schema_migrations" in sql for sql in server.statements), server.statements
    assert not any(sql.startswith("INSERT") for sql in server.statements)
    assert server.applied == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    assert first_pass != server.statements


def test_concurrent_migrators_do_not_race():
    server = FakeServer()
    database = FakeDatabase(server)
    start = threading.Barrier(8)

    def migrate():
        start.wait()
        run_collab_schema_migrations(database)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(migrate) for _ in range(8)]:
            future.result()

    # Each version recorded exactly once (FakeServer raises on a repeat), and
    # each table's DDL run exactly once by whichever replica won the lock.
    assert server.applied == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    assert len(server.ddl_for("collab_orgs")) == 1
    assert len(server.ddl_for("collab_org_members")) == 1


def test_migration_versions_are_append_only_and_start_at_one():
    versions = [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1
    assert LATEST_COLLAB_SCHEMA_VERSION == versions[-1]


@pytest.mark.parametrize(
    "versions",
    [
        (2,),  # must start at 1, not merely at a positive number
        (1, 1),  # duplicated
        (2, 1),  # renumbered/reordered
        (1, 3, 2),
    ],
)
def test_import_time_validator_rejects_broken_version_numbering(monkeypatch, versions):
    monkeypatch.setattr(
        collab_schema,
        "COLLAB_SCHEMA_MIGRATIONS",
        tuple((version, ()) for version in versions),
    )
    with pytest.raises(RuntimeError, match="unique, ascending versions starting at 1"):
        collab_schema._validate_migrations()


def test_startup_runs_the_migration_only_when_auto_migrate_and_a_url_are_set(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(
        "collab_hub_api.config.run_collab_schema_migrations",
        lambda db: ran.append(db.database_url),
    )

    def build(postgres: dict) -> Config:
        return Config.parse({"frames": {"postgres": postgres}})

    for postgres in ({}, {"url": "postgresql://example/db"}, {"auto_migrate": True}):
        config = build(postgres)
        assert migrate_collab_schema(config, build_postgres_pools(config)) is False
    assert ran == []

    config = build({"url": "postgresql://example/db", "auto_migrate": True})
    assert migrate_collab_schema(config, build_postgres_pools(config)) is True
    assert ran == ["postgresql://example/db"]


# --------------------------------------------------------------------------
# Startup version preflight (issue #96)
# --------------------------------------------------------------------------


class VersionConnection:
    """Answers the one query the version read issues, or fails like Postgres."""

    def __init__(self, version: int | None, error: Exception | None):
        self.version = version
        self.error = error
        self._pending: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        if self.error is not None:
            raise self.error
        self._pending = {"version": self.version}
        return self

    def fetchone(self):
        return self._pending


class VersionDatabase:
    database_url = "postgresql://stub/example"

    def __init__(self, version: int | None = None, error: Exception | None = None):
        self.version = version
        self.error = error

    def connection(self, timeout=None):
        return VersionConnection(self.version, self.error)


def test_preflight_accepts_a_current_schema():
    assert check_collab_schema_version(VersionDatabase(LATEST_COLLAB_SCHEMA_VERSION), auto_migrate=False) == (
        LATEST_COLLAB_SCHEMA_VERSION
    )


def test_preflight_refuses_a_schema_behind_this_build():
    # The whole point: an operator who has to run a migration is told so at
    # startup instead of finding a "relation does not exist" traceback in a
    # request log later.
    with pytest.raises(CollabSchemaVersionError, match="auto-migration|Auto-migration"):
        check_collab_schema_version(VersionDatabase(0), auto_migrate=False)


def test_preflight_names_auto_migration_when_it_was_supposed_to_run():
    with pytest.raises(CollabSchemaVersionError, match="did not take effect"):
        check_collab_schema_version(VersionDatabase(0), auto_migrate=True)


def test_preflight_tolerates_a_schema_ahead_of_this_build():
    # An ordinary rolling update: the new replica migrates while old ones still
    # serve. Fatal here would make every deploy an outage and block rollbacks.
    ahead = LATEST_COLLAB_SCHEMA_VERSION + 1
    assert check_collab_schema_version(VersionDatabase(ahead), auto_migrate=False) == ahead


def test_preflight_tolerates_an_unreachable_database():
    # The pool is built so a Postgres outage at startup does not crash the app;
    # failing here would turn a transient outage into a crash loop. Nothing is
    # asserted about the schema — the check simply could not run.
    database = VersionDatabase(error=psycopg.OperationalError("connection refused"))
    assert check_collab_schema_version(database, auto_migrate=False) is None


def test_preflight_is_skipped_without_a_shared_postgres_url():
    config = Config.parse({"frames": {"postgres": {}}})
    assert preflight_collab_schema(config, build_postgres_pools(config)) is None


# --------------------------------------------------------------------------
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres migration tests",
)


def _database(max_size: int = 10):
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=10.0)


@pytest.fixture
def clean_database():
    """A live database with every ``collab_`` object dropped, before and after."""

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    database = _database()
    try:
        drop_all()
        yield database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_migration_creates_tables_constraints_and_index(clean_database):
    run_collab_schema_migrations(clean_database)

    with clean_database.connection() as conn:
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
        assert set(COLLAB_TABLES) <= tables

        orgs = {
            row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"])
            for row in conn.execute(
                "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
                " WHERE table_name = 'collab_orgs'"
            ).fetchall()
        }
        assert set(orgs) == {"id", "name", "created_at", "created_by"}
        assert orgs["id"][0] == "text" and orgs["id"][1] == "NO"
        assert orgs["name"][1] == "YES" and "Unnamed organization" in orgs["name"][2]
        assert orgs["created_by"][1] == "NO"

        members = {
            row["column_name"]: row["is_nullable"]
            for row in conn.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_name = 'collab_org_members'"
            ).fetchall()
        }
        assert set(members) == {"user_id", "org_id", "role", "email", "display_name", "created_at", "status"}
        assert members["email"] == "YES" and members["display_name"] == "YES"

        index_rows = conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'collab_org_members'").fetchall()
        indexes = {row["indexname"] for row in index_rows}
        assert "collab_org_members_org" in indexes

        assert applied_collab_schema_version(clean_database) == LATEST_COLLAB_SCHEMA_VERSION


@live_postgres
def test_live_constraints_enforce_the_membership_invariants(clean_database):
    run_collab_schema_migrations(clean_database)

    with clean_database.connection() as conn:
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES ('org-1', 'sub-owner')")
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES ('org-2', 'sub-owner')")
        # The placeholder default applies when no name is supplied.
        assert conn.execute("SELECT name FROM collab_orgs WHERE id = 'org-1'").fetchone()["name"] == (
            "Unnamed organization"
        )
        # Non-unique display names.
        conn.execute("UPDATE collab_orgs SET name = 'Acme'")
        conn.execute("INSERT INTO collab_org_members (user_id, org_id, role) VALUES ('sub-owner', 'org-1', 'owner')")
        assert conn.execute("SELECT status FROM collab_org_members").fetchone()["status"] == "active"

    # One home org per login: a second membership row for the same sub is
    # rejected even when it names a different org.
    second_home_org = "INSERT INTO collab_org_members (user_id, org_id, role) VALUES ('sub-owner', 'org-2', 'member')"
    with pytest.raises(psycopg.errors.UniqueViolation):
        with clean_database.connection() as conn:
            conn.execute(second_home_org)

    for column, value in (("role", "admin"), ("status", "deleted")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with clean_database.connection() as conn:
                conn.execute(f"UPDATE collab_org_members SET {column} = %s WHERE user_id = 'sub-owner'", (value,))

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with clean_database.connection() as conn:
            conn.execute("INSERT INTO collab_org_members (user_id, org_id, role) VALUES ('sub-x', 'missing', 'member')")

    # Removal retains the row, so the home-org binding stays enforceable.
    with clean_database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET status = 'removed' WHERE user_id = 'sub-owner'")
        row = conn.execute("SELECT org_id, status FROM collab_org_members WHERE user_id = 'sub-owner'").fetchone()
    assert row == {"org_id": "org-1", "status": "removed"}


@live_postgres
def test_live_constraints_enforce_the_operator_and_audit_invariants(clean_database):
    run_collab_schema_migrations(clean_database)

    with clean_database.connection() as conn:
        # The documented bootstrap shape: role defaults nothing, status
        # defaults to 'active', granted_by may be NULL.
        conn.execute("INSERT INTO collab_platform_roles (user_id, role) VALUES ('sub-op', 'operator')")
        row = conn.execute(
            "SELECT role, status, granted_by FROM collab_platform_roles WHERE user_id = 'sub-op'"
        ).fetchone()
        assert row == {"role": "operator", "status": "active", "granted_by": None}

    # psql is the write path for platform roles in this beta, so the CHECKs
    # are what stand between a typo and a silent non-grant.
    for statement in (
        "INSERT INTO collab_platform_roles (user_id, role) VALUES ('sub-x', 'admin')",
        "UPDATE collab_platform_roles SET status = 'disabled' WHERE user_id = 'sub-op'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with clean_database.connection() as conn:
                conn.execute(statement)

    # One row per sub: a second grant for the same login is an update, not an
    # accumulation.
    with pytest.raises(psycopg.errors.UniqueViolation):
        with clean_database.connection() as conn:
            conn.execute("INSERT INTO collab_platform_roles (user_id, role) VALUES ('sub-op', 'operator')")

    with clean_database.connection() as conn:
        # Audit rows: actor and action are required; targets, org, and labels
        # genuinely nullable (hub-scoped events belong to no org); at and id
        # are generated.
        row = conn.execute(
            "INSERT INTO collab_audit_events (actor, action) VALUES ('sub-op', 'operator.manual')"
            " RETURNING id, at, actor_label, target_type, target_id, target_label, org_id, detail"
        ).fetchone()
        assert row["id"] == 1 and row["at"] is not None
        assert all(
            row[column] is None
            for column in ("actor_label", "target_type", "target_id", "target_label", "org_id", "detail")
        )

    with pytest.raises(psycopg.errors.NotNullViolation):
        with clean_database.connection() as conn:
            conn.execute("INSERT INTO collab_audit_events (action) VALUES ('operator.manual')")

    # The action and target-type vocabularies are enforced by the schema, so
    # a typo'd hand-run psql insert is refused instead of creating a row no
    # runbook query ever finds.
    for statement in (
        "INSERT INTO collab_audit_events (actor, action) VALUES ('sub-op', 'org.obliterate')",
        "INSERT INTO collab_audit_events (actor, action, target_type) VALUES ('sub-op', 'org.create', 'frame')",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with clean_database.connection() as conn:
                conn.execute(statement)


@live_postgres
def test_live_migration_is_idempotent_across_restarts(clean_database):
    run_collab_schema_migrations(clean_database)
    with clean_database.connection() as conn:
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES ('org-keep', 'sub-owner')")

    run_collab_schema_migrations(clean_database)
    run_collab_schema_migrations(clean_database)

    with clean_database.connection() as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM collab_schema_migrations").fetchall()]
        # Existing data survives; nothing was recreated.
        assert conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()["n"] == 1
    assert versions == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]


@live_postgres
def test_live_failed_migration_leaves_neither_schema_nor_version_row(clean_database, monkeypatch):
    """A migration that dies part-way must leave the database untouched.

    This is the property the whole run — lock, DDL, and version bookkeeping in
    one transaction — exists to provide, and the one the fake server cannot
    express: it records statements as they are issued and has no notion of
    commit. Here the last statement of the (patched) version fails, so Postgres
    must roll back the tables *and* the version row together. The dangerous
    alternative is a database that reports version 1 applied while the tables
    it describes do not exist — every later startup would then skip the
    migration and the pod would fail at request time instead.
    """

    real_statements = COLLAB_SCHEMA_MIGRATIONS[0][1]
    monkeypatch.setattr(
        collab_schema,
        "COLLAB_SCHEMA_MIGRATIONS",
        ((1, (*real_statements, "CREATE TABLE collab_orgs_boom (id text)", "SELECT no_such_function_exists()")),),
    )

    with pytest.raises(psycopg.errors.UndefinedFunction):
        run_collab_schema_migrations(clean_database)

    with clean_database.connection() as conn:
        surviving = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
                " AND table_name LIKE 'collab%'"
            ).fetchall()
        }
    # Nothing at all: not the tables the migration created before failing, and
    # not the bookkeeping table that would have claimed the version.
    assert surviving == set()
    assert applied_collab_schema_version(clean_database) == 0

    # And the database is left in a state a corrected build migrates cleanly.
    monkeypatch.undo()
    run_collab_schema_migrations(clean_database)
    assert applied_collab_schema_version(clean_database) == LATEST_COLLAB_SCHEMA_VERSION


@live_postgres
def test_live_preflight_reads_the_real_version_table(clean_database):
    # An unmigrated database is "behind" even though the bookkeeping table does
    # not exist yet — which is precisely the autoMigrate:false case issue #96
    # is about.
    with pytest.raises(CollabSchemaVersionError):
        check_collab_schema_version(clean_database, auto_migrate=False)

    run_collab_schema_migrations(clean_database)

    assert check_collab_schema_version(clean_database, auto_migrate=False) == LATEST_COLLAB_SCHEMA_VERSION


@live_postgres
def test_live_concurrent_startup_does_not_race(clean_database):
    replicas = 8
    databases = [_database(max_size=2) for _ in range(replicas)]
    start = threading.Barrier(replicas)

    def migrate(database):
        start.wait()
        run_collab_schema_migrations(database)

    try:
        with ThreadPoolExecutor(max_workers=replicas) as pool:
            futures = [pool.submit(migrate, database) for database in databases]
            for future in futures:
                # A raced CREATE TABLE IF NOT EXISTS surfaces here as a
                # duplicate-key error on pg_type/pg_class.
                future.result()
    finally:
        for database in databases:
            database.close()

    with clean_database.connection() as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM collab_schema_migrations").fetchall()]
    assert versions == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
