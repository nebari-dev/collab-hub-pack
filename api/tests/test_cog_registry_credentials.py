"""Registry credentials and pull tokens (issue #179): the store contract, on every backend.

- the in-memory store, always;
- the Postgres store against a fake connection, always: each method issues
  the statement it claims with the parameters it claims, and maps rows back;
- the Postgres store against a live database, opt-in with
  ``COLLAB_HUB_TEST_POSTGRES_URL``, where the same contract is proven on the
  real tables -- expiry on the server's clock, the cascade that makes
  revocation immediate, and a token that cannot outlive its credential.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_api.cogs.registry_credentials import (
    CREDENTIAL_ID_PREFIX,
    CREDENTIAL_SECRET_PREFIX,
    MAX_CREDENTIALS_PER_USER,
    PULL_TOKEN_PREFIX,
    InMemoryRegistryCredentialStore,
    PostgresRegistryCredentialStore,
    RegistryCredential,
    RegistryCredentialsUnavailableError,
    TokenGrant,
    UnavailableRegistryCredentialStore,
    new_credential_id,
    new_credential_secret,
    new_pull_token,
    secret_digest,
)
from collab_hub_api.config import Config, build_cog_registry_credential_store
from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
from collab_hub_api.frames.db import PostgresPools

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_generated_values_are_distinct_prefixed_and_stored_as_digests():
    ids = {new_credential_id() for _ in range(50)}
    secrets_ = {new_credential_secret() for _ in range(50)}
    tokens = {new_pull_token() for _ in range(50)}
    assert len(ids) == len(secrets_) == len(tokens) == 50
    assert all(value.startswith(CREDENTIAL_ID_PREFIX) for value in ids)
    assert all(value.startswith(CREDENTIAL_SECRET_PREFIX) and len(value) > 40 for value in secrets_)
    assert all(value.startswith(PULL_TOKEN_PREFIX) and len(value) > 40 for value in tokens)
    secret = next(iter(secrets_))
    assert len(secret_digest(secret)) == 64 and secret not in secret_digest(secret)
    assert secret_digest(secret) == secret_digest(secret) != secret_digest(secret + "x")


def test_token_grant_allows_only_its_repositories():
    grant = TokenGrant(user_id="u", repositories=("cogs/a",), expires_at=T0, issued_at=T0)
    assert grant.allows_pull("cogs/a") and not grant.allows_pull("cogs/b")


def test_the_unavailable_store_refuses_every_call():
    store = UnavailableRegistryCredentialStore()
    calls = (
        lambda: store.create_credential(
            credential_id="c", user_id="u", secret_hash="h", scope="pull", session_id=None, ttl_seconds=1
        ),
        lambda: store.find_credential("c", "h"),
        lambda: store.revoke_credential("c", "u"),
        lambda: store.revoke_all("u"),
        lambda: store.create_token(token_hash="t", user_id="u", credential_id=None, repositories=(), ttl_seconds=1),
        lambda: store.find_token("t"),
    )
    for call in calls:
        with pytest.raises(RegistryCredentialsUnavailableError, match="not configured"):
            call()


def test_the_store_follows_the_catalog_backend(tmp_path):
    def build(cogs: dict, postgres_url: str = ""):
        config = Config.parse({"cogs": cogs, "frames": {"postgres": {"url": postgres_url}}})
        return build_cog_registry_credential_store(config, PostgresPools())

    assert isinstance(build({"catalog": {"backend": "memory"}}), InMemoryRegistryCredentialStore)
    assert isinstance(build({}), UnavailableRegistryCredentialStore)
    assert isinstance(build({}, "postgresql://db.example/collab"), PostgresRegistryCredentialStore)


# ---------------------------------------------------------------------------
# The contract, run against the in-memory store and (opt in) live Postgres.
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")


class _Clock:
    """Moves "now" for the in-memory store; the live store's rows are moved instead."""

    def __init__(self, store, database=None) -> None:
        self.store = store
        self.database = database
        self.now = datetime.now(UTC)
        if database is None:
            store.clock = lambda: self.now

    def advance(self, seconds: float) -> None:
        if self.database is None:
            self.now += timedelta(seconds=seconds)
            return
        with self.database.connection() as conn:
            for table in ("collab_cog_registry_credentials", "collab_cog_registry_tokens"):
                conn.execute(
                    f"UPDATE {table} SET created_at = created_at - make_interval(secs => %s),"
                    " expires_at = expires_at - make_interval(secs => %s)",
                    (seconds, seconds),
                )


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    if request.param == "memory":
        store = InMemoryRegistryCredentialStore()
        yield store, _Clock(store)
        return
    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=10.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        store = PostgresRegistryCredentialStore(database)
        yield store, _Clock(store, database)
    finally:
        drop_all()
        database.close()


def _credential(store, user="alice", ttl=900, credential_id=None, secret="s3cret", session_id="sid-1"):
    return store.create_credential(
        credential_id=credential_id or new_credential_id(),
        user_id=user,
        secret_hash=secret_digest(secret),
        scope="pull",
        session_id=session_id,
        ttl_seconds=ttl,
    )


def _token(store, credential, *, user=None, repositories=("cogs/a",), ttl=300, token=None):
    token = token or new_pull_token()
    grant = store.create_token(
        token_hash=secret_digest(token),
        user_id=user or (credential.user_id if credential else "alice"),
        credential_id=credential.id if credential else None,
        repositories=repositories,
        ttl_seconds=ttl,
    )
    return token, grant


def test_a_credential_is_found_only_by_its_id_and_secret(backend):
    store, _clock = backend
    created = _credential(store)
    assert isinstance(created, RegistryCredential)
    assert (created.user_id, created.scope, created.session_id) == ("alice", "pull", "sid-1")
    assert timedelta(seconds=899) <= created.expires_at - created.created_at <= timedelta(seconds=901)

    found = store.find_credential(created.id, secret_digest("s3cret"))
    assert found == created
    assert store.find_credential(created.id, secret_digest("wrong")) is None
    assert store.find_credential("crc-nobody", secret_digest("s3cret")) is None


def test_a_credential_expires(backend):
    store, clock = backend
    created = _credential(store, ttl=60)
    clock.advance(59)
    assert store.find_credential(created.id, secret_digest("s3cret")) is not None
    clock.advance(2)
    assert store.find_credential(created.id, secret_digest("s3cret")) is None
    assert store.revoke_credential(created.id, "alice") is False, "an expired credential is already gone"
    token, grant = _token(store, created)
    assert grant is None and store.find_token(secret_digest(token)) is None


def test_revoking_is_owner_only_and_takes_the_tokens_with_it(backend):
    store, _clock = backend
    mine, theirs = _credential(store), _credential(store, user="bob")
    token, grant = _token(store, mine)
    their_token, _ = _token(store, theirs)
    assert grant.credential_id == mine.id and grant.repositories == ("cogs/a",)
    assert store.find_token(secret_digest(token)) == grant

    assert store.revoke_credential(mine.id, "bob") is False
    assert store.find_token(secret_digest(token)) is not None
    assert store.revoke_credential(mine.id, "alice") is True
    assert store.find_token(secret_digest(token)) is None
    assert store.find_credential(mine.id, secret_digest("s3cret")) is None
    assert store.revoke_credential(mine.id, "alice") is False
    assert store.find_token(secret_digest(their_token)) is not None


def test_revoke_all_covers_credentials_and_directly_minted_tokens(backend):
    store, _clock = backend
    first, second, theirs = _credential(store), _credential(store), _credential(store, user="bob")
    tokens = [_token(store, first)[0], _token(store, second)[0], _token(store, None, user="alice")[0]]
    their_token, _ = _token(store, theirs)
    their_direct, _ = _token(store, None, user="bob")

    assert store.revoke_all("alice") == 2
    assert [store.find_token(secret_digest(token)) for token in tokens] == [None, None, None]
    assert store.find_token(secret_digest(their_token)) is not None
    assert store.find_token(secret_digest(their_direct)) is not None
    assert store.revoke_all("alice") == 0


def test_a_token_never_outlives_its_credential(backend):
    store, clock = backend
    credential = _credential(store, ttl=120)
    clock.advance(60)
    token, grant = _token(store, credential, ttl=300)
    assert timedelta(seconds=58) <= grant.expires_at - grant.issued_at <= timedelta(seconds=61)
    assert grant.expires_at == store.find_credential(credential.id, secret_digest("s3cret")).expires_at
    clock.advance(61)
    assert store.find_token(secret_digest(token)) is None


def test_a_token_expires_on_its_own_and_a_direct_one_has_no_credential(backend):
    store, clock = backend
    credential = _credential(store, ttl=900)
    token, grant = _token(store, credential, ttl=60, repositories=("cogs/a", "cogs/b"))
    direct, direct_grant = _token(store, None, user="alice", ttl=60, repositories=())
    assert grant.repositories == ("cogs/a", "cogs/b")
    assert direct_grant.credential_id is None and direct_grant.repositories == ()
    assert timedelta(seconds=59) <= grant.expires_at - grant.issued_at <= timedelta(seconds=61)
    clock.advance(61)
    assert store.find_token(secret_digest(token)) is None
    assert store.find_token(secret_digest(direct)) is None
    assert store.find_token(secret_digest("never-issued")) is None
    # A token cannot be minted against somebody else's credential.
    _other, refused = _token(store, credential, user="bob")
    assert refused is None


def test_exchanging_past_the_cap_drops_the_oldest(backend):
    store, clock = backend
    created = []
    for _ in range(MAX_CREDENTIALS_PER_USER + 2):
        created.append(_credential(store))
        clock.advance(1)
    other = _credential(store, user="bob")
    alive = [c for c in created if store.find_credential(c.id, secret_digest("s3cret")) is not None]
    assert alive == created[2:]
    assert store.find_credential(other.id, secret_digest("s3cret")) is not None


def test_expired_rows_are_swept_by_the_next_write(backend):
    store, clock = backend
    old = _credential(store, ttl=60)
    old_token, _ = _token(store, old, ttl=30)
    direct, _ = _token(store, None, user="alice", ttl=30)
    clock.advance(120)
    if isinstance(store, InMemoryRegistryCredentialStore):
        # A read is enough to sweep: a Hub that has gone quiet does not keep expired rows.
        assert store.find_token(secret_digest(old_token)) is None
        assert store._credentials == {} and store._tokens == {}
    _credential(store, user="bob")
    _token(store, None, user="bob")
    if isinstance(store, InMemoryRegistryCredentialStore):
        assert old.id not in store._credentials
        assert secret_digest(old_token) not in store._tokens and secret_digest(direct) not in store._tokens
    else:
        with store._db.connection() as conn:
            credentials = conn.execute("SELECT count(*) AS n FROM collab_cog_registry_credentials").fetchone()["n"]
            tokens = conn.execute("SELECT count(*) AS n FROM collab_cog_registry_tokens").fetchone()["n"]
        assert (credentials, tokens) == (1, 1)


# ---------------------------------------------------------------------------
# The Postgres store against a fake connection.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Records every statement; answers a statement whose text contains a scripted marker."""

    def __init__(self, answers: dict[str, list] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.budgets: list[int] = []
        self.answers = answers or {}

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if text.startswith("SELECT set_config('statement_timeout'"):
            # Every call's preamble (the request budget); asserted once, below.
            self.budgets.append(int(params[0]))
            return _FakeResult([])
        self.calls.append((text, tuple(params or ())))
        for marker, rows in self.answers.items():
            if marker in text:
                return _FakeResult(rows)
        return _FakeResult([])


class _FakeDb:
    def __init__(self, conn):
        self.conn = conn
        self.acquire_timeouts: list[float | None] = []

    @contextmanager
    def connection(self, timeout=None):
        self.acquire_timeouts.append(timeout)
        yield self.conn


def _fake(answers=None):
    conn = _FakeConnection(answers)
    return PostgresRegistryCredentialStore(_FakeDb(conn)), conn


CREDENTIAL_ROW = {
    "id": "crc-1",
    "user_id": "alice",
    "scope": "pull",
    "session_id": "sid-1",
    "created_at": T0,
    "expires_at": T0 + timedelta(minutes=15),
}
TOKEN_ROW = {
    "user_id": "alice",
    "credential_id": "crc-1",
    "repositories": ["cogs/a"],
    "created_at": T0,
    "expires_at": T0 + timedelta(minutes=5),
}


def test_postgres_create_credential_sweeps_inserts_and_caps():
    store, conn = _fake({"INSERT INTO collab_cog_registry_credentials": [CREDENTIAL_ROW]})
    created = store.create_credential(
        credential_id="crc-1", user_id="alice", secret_hash="hash", scope="pull", session_id="sid-1", ttl_seconds=900
    )
    assert created == RegistryCredential(
        id="crc-1",
        user_id="alice",
        scope="pull",
        session_id="sid-1",
        created_at=T0,
        expires_at=T0 + timedelta(minutes=15),
    )
    sweep_credentials, sweep_tokens, insert, cap = conn.calls
    assert sweep_credentials[0] == "DELETE FROM collab_cog_registry_credentials WHERE expires_at <= now()"
    assert sweep_tokens[0] == "DELETE FROM collab_cog_registry_tokens WHERE expires_at <= now()"
    assert "now() + make_interval(secs => %s)" in insert[0]
    assert insert[1] == ("crc-1", "alice", "hash", "pull", "sid-1", 900)
    assert "ORDER BY created_at DESC, id DESC LIMIT %s" in cap[0]
    assert cap[1] == ("alice", "alice", MAX_CREDENTIALS_PER_USER)


def test_postgres_find_credential_matches_id_digest_and_liveness():
    store, conn = _fake({"FROM collab_cog_registry_credentials": [CREDENTIAL_ROW]})
    assert store.find_credential("crc-1", "hash").id == "crc-1"
    ((sql, params),) = conn.calls
    assert "WHERE id = %s AND secret_hash = %s AND expires_at > now()" in sql
    assert params == ("crc-1", "hash")
    empty, _ = _fake()
    assert empty.find_credential("crc-1", "hash") is None


def test_postgres_revocation_deletes_rows():
    store, conn = _fake({"DELETE FROM collab_cog_registry_credentials WHERE id": [{"id": "crc-1"}]})
    assert store.revoke_credential("crc-1", "alice") is True
    ((sql, params),) = conn.calls
    assert sql.endswith("WHERE id = %s AND user_id = %s AND expires_at > now() RETURNING id")
    assert params == ("crc-1", "alice")
    missing, _ = _fake()
    assert missing.revoke_credential("crc-1", "alice") is False

    store, conn = _fake({"DELETE FROM collab_cog_registry_credentials WHERE user_id": [{"id": "a"}, {"id": "b"}]})
    assert store.revoke_all("alice") == 2
    credentials, tokens = conn.calls
    assert credentials == ("DELETE FROM collab_cog_registry_credentials WHERE user_id = %s RETURNING id", ("alice",))
    assert tokens == ("DELETE FROM collab_cog_registry_tokens WHERE user_id = %s", ("alice",))


def test_postgres_create_token_is_one_statement_with_the_liveness_check():
    store, conn = _fake({"INSERT INTO collab_cog_registry_tokens": [TOKEN_ROW]})
    grant = store.create_token(
        token_hash="thash", user_id="alice", credential_id="crc-1", repositories=("cogs/a",), ttl_seconds=300
    )
    assert grant == TokenGrant(
        user_id="alice",
        credential_id="crc-1",
        repositories=("cogs/a",),
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=5),
    )
    sweep, insert = conn.calls
    assert sweep[0] == "DELETE FROM collab_cog_registry_tokens WHERE expires_at <= now()"
    assert "LEAST(now() + make_interval(secs => %s), c.expires_at)" in insert[0]
    assert "WHERE c.id = %s AND c.user_id = %s AND c.expires_at > now()" in insert[0]
    assert insert[1] == ("thash", ["cogs/a"], 300, "crc-1", "alice")
    # The credential went away between the caller's check and the insert.
    gone, _ = _fake()
    assert (
        gone.create_token(token_hash="t", user_id="alice", credential_id="crc-1", repositories=(), ttl_seconds=1)
        is None
    )


def test_postgres_create_token_without_a_credential():
    row = {**TOKEN_ROW, "credential_id": None, "repositories": None}
    store, conn = _fake({"INSERT INTO collab_cog_registry_tokens": [row]})
    grant = store.create_token(
        token_hash="thash", user_id="alice", credential_id=None, repositories=iter(()), ttl_seconds=300
    )
    assert grant.credential_id is None and grant.repositories == ()
    _sweep, insert = conn.calls
    assert "VALUES (%s, %s, NULL, %s, now() + make_interval(secs => %s))" in insert[0]
    assert insert[1] == ("thash", "alice", [], 300)


def test_postgres_reads_sweep_expired_rows_at_most_once_per_interval(monkeypatch):
    from collab_hub_api.cogs import registry_credentials

    clock = [1000.0]
    monkeypatch.setattr(registry_credentials.time, "monotonic", lambda: clock[0])
    store, conn = _fake()
    store.find_token("thash")
    assert len(conn.calls) == 1, "nothing to sweep yet: the store was only just built"
    clock[0] += registry_credentials.SWEEP_INTERVAL_SECONDS + 1
    store.find_token("thash")
    assert [sql for sql, _ in conn.calls[1:3]] == [
        "DELETE FROM collab_cog_registry_credentials WHERE expires_at <= now()",
        "DELETE FROM collab_cog_registry_tokens WHERE expires_at <= now()",
    ]
    store.find_token("thash")
    assert len(conn.calls) == 5, "and not again until the interval has passed"


def test_postgres_calls_are_bounded_by_the_request_budget():
    from collab_hub_api.cogs import deadline

    store, conn = _fake()
    calls = (
        lambda: store.find_token("t"),
        lambda: store.find_credential("c", "h"),
        lambda: store.revoke_credential("c", "u"),
        lambda: store.revoke_all("u"),
        lambda: store.create_token(token_hash="t", user_id="u", credential_id=None, repositories=(), ttl_seconds=1),
    )
    for call in calls:
        call()
    # Outside a request: the default budget, for the pool wait and before every single statement.
    default_ms = int(deadline.DEFAULT_BUDGET_SECONDS * 1000)
    assert len(conn.budgets) == len(conn.calls) and all(default_ms - 1000 < ms <= default_ms for ms in conn.budgets)
    assert store._db.acquire_timeouts == [deadline.DEFAULT_BUDGET_SECONDS] * len(calls)

    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.5)
    try:
        store.find_token("t")
        assert 1 <= conn.budgets[-1] <= 500 and 0 < store._db.acquire_timeouts[-1] <= 0.5
        deadline.request_deadline.set(deadline.time.monotonic() - 1)
        before = len(store._db.acquire_timeouts)
        with pytest.raises(deadline.BudgetExhausted):
            store.find_token("t")
        assert len(store._db.acquire_timeouts) == before, "a spent budget does not even take a connection"
    finally:
        deadline.request_deadline.reset(token)


def test_the_budget_is_spent_once_across_the_pool_wait_and_every_statement(monkeypatch):
    """Waiting for a connection and running earlier statements both use up what later statements get."""

    from collab_hub_api.cogs import deadline

    clock = [100.0]
    monkeypatch.setattr(deadline.time, "monotonic", lambda: clock[0])
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append((sql, tuple(params or ())))
            if not sql.startswith("SELECT set_config"):
                clock[0] += 4.0  # each statement takes four seconds
            return self

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            assert timeout == 30.0
            clock[0] += 29.0  # the pool made us wait for nearly all of it
            yield Connection()

    token = deadline.request_deadline.set(clock[0] + 30.0)
    try:
        with pytest.raises(deadline.BudgetExhausted):
            with deadline.bounded_connection(Database()) as conn:
                conn.execute("SELECT 1")
                conn.execute("SELECT 2")
    finally:
        deadline.request_deadline.reset(token)
    # One second was left after the checkout, not a fresh thirty; the second statement was never sent.
    assert statements == [
        ("SELECT set_config('statement_timeout', %s, true)", ("1000",)),
        ("SELECT 1", ()),
    ]


def test_request_connection_is_the_ordinary_checkout_outside_a_request():
    from collab_hub_api.cogs import deadline

    conn = _FakeConnection()
    db = _FakeDb(conn)
    with deadline.request_connection(db) as plain:
        plain.execute("SELECT 1")
    assert plain is conn and conn.budgets == [] and db.acquire_timeouts == [None]

    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.5)
    try:
        with deadline.request_connection(db) as bounded:
            bounded.execute("SELECT 1")
    finally:
        deadline.request_deadline.reset(token)
    assert isinstance(bounded, deadline.BudgetedConnection)
    assert 0 < db.acquire_timeouts[-1] <= 0.5 and 1 <= conn.budgets[-1] <= 500


def test_postgres_find_token_requires_a_live_credential_when_it_has_one():
    store, conn = _fake({"FROM collab_cog_registry_tokens t": [TOKEN_ROW]})
    assert store.find_token("thash").repositories == ("cogs/a",)
    ((sql, params),) = conn.calls
    assert "LEFT JOIN collab_cog_registry_credentials c ON c.id = t.credential_id" in sql
    assert "t.expires_at > now() AND (t.credential_id IS NULL OR c.expires_at > now())" in sql
    assert params == ("thash",)
    empty, _ = _fake()
    assert empty.find_token("thash") is None
