"""Repository ownership and upload sessions (issue #180): the store contract, on every backend.

In memory always; Postgres against a fake connection always (the statements
and their parameters); Postgres live when ``COLLAB_HUB_TEST_POSTGRES_URL``
names a disposable database -- where the atomic first-claim and the
per-user cap under concurrency are actually proven.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_api.cogs import deadline
from collab_hub_api.cogs.publish_store import (
    MAX_UPLOAD_SESSIONS_PER_USER,
    UPLOAD_ID_PREFIX,
    UPLOAD_OPEN_LOCK_CLASS,
    UPLOAD_SESSION_TTL_SECONDS,
    InMemoryPublishStore,
    PostgresPublishStore,
    PublishStoreUnavailableError,
    RepositoryRecord,
    UnavailablePublishStore,
    UploadSession,
    new_upload_id,
)
from collab_hub_api.config import Config, build_cog_publish_store
from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
from collab_hub_api.frames.db import PostgresPools

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")
LOCATION = "https://backing.internal/v2/cogs/a/blobs/uploads/u1?_state=secret"


def test_upload_ids_are_one_url_safe_segment_and_unguessable():
    ids = {new_upload_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(value.startswith(UPLOAD_ID_PREFIX) and len(value) == len(UPLOAD_ID_PREFIX) + 32 for value in ids)
    assert all(value.replace("-", "").isalnum() for value in ids)


def test_the_unavailable_store_refuses_every_call():
    store = UnavailablePublishStore()
    calls = (
        lambda: store.get_repository("cogs/a"),
        lambda: store.claim_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u"),
        lambda: store.published_repositories("s"),
        lambda: store.open_upload(
            upload_id="up-1", user_id="u", repository="cogs/a", source_id="s", upstream_location=LOCATION
        ),
        lambda: store.get_upload("up-1", user_id="u", repository="cogs/a"),
        lambda: store.advance_upload("up-1", expected_received=0, received=1, upstream_location=LOCATION),
        lambda: store.close_upload("up-1"),
    )
    for call in calls:
        with pytest.raises(PublishStoreUnavailableError, match="not configured"):
            call()


def test_the_store_follows_the_catalog_backend():
    def build(cogs: dict, postgres_url: str = ""):
        config = Config.parse({"cogs": cogs, "frames": {"postgres": {"url": postgres_url}}})
        return build_cog_publish_store(config, PostgresPools())

    assert isinstance(build({"catalog": {"backend": "memory"}}), InMemoryPublishStore)
    assert isinstance(build({}), UnavailablePublishStore)
    assert isinstance(build({}, "postgresql://db.example/collab"), PostgresPublishStore)


class _Clock:
    def __init__(self, store, database=None) -> None:
        self.database = database
        self.now = datetime.now(UTC)
        if database is None:
            store.clock = lambda: self.now

    def advance(self, seconds: float) -> None:
        if self.database is None:
            self.now += timedelta(seconds=seconds)
            return
        with self.database.connection() as conn:
            conn.execute(
                "UPDATE collab_cog_upload_sessions SET created_at = created_at - make_interval(secs => %s),"
                " expires_at = expires_at - make_interval(secs => %s)",
                (seconds, seconds),
            )


def _live_database(max_size: int = 4):
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=30.0)


def _drop_all(database) -> None:
    from test_collab_schema import COLLAB_TABLES

    with database.connection() as conn:
        for table in COLLAB_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    if request.param == "memory":
        store = InMemoryPublishStore()
        yield store, _Clock(store)
        return
    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    database = _live_database()
    try:
        _drop_all(database)
        run_collab_schema_migrations(database)
        store = PostgresPublishStore(database)
        yield store, _Clock(store, database)
    finally:
        _drop_all(database)
        database.close()


def _open(store, user="alice", repository="cogs/a", upload_id=None) -> UploadSession:
    return store.open_upload(
        upload_id=upload_id or new_upload_id(),
        user_id=user,
        repository=repository,
        source_id="backing",
        upstream_location=LOCATION,
    )


def test_the_first_claim_owns_the_repository_and_later_ones_read_it(backend):
    store, _clock = backend
    assert store.get_repository("cogs/a") is None
    first = store.claim_repository("cogs/a", source_id="backing", owner_org_id="org-a", created_by="alice")
    assert first == RepositoryRecord(repository="cogs/a", source_id="backing", owner_org_id="org-a", created_by="alice")
    # A second claim changes nothing and is told who owns it.
    second = store.claim_repository("cogs/a", source_id="other", owner_org_id="org-b", created_by="bob")
    assert second == first and store.get_repository("cogs/a") == first
    # An operator with no organization may own a repository; it then belongs to no organization.
    orphan = store.claim_repository("cogs/op", source_id="backing", owner_org_id=None, created_by="operator")
    assert orphan.owner_org_id is None
    store.claim_repository("cogs/b", source_id="mirror", owner_org_id="org-a", created_by="alice")
    assert store.published_repositories("backing") == ["cogs/a", "cogs/op"]
    assert store.published_repositories("mirror") == ["cogs/b"] and store.published_repositories("none") == []


def test_an_upload_is_found_only_by_its_owner_for_its_repository(backend):
    store, _clock = backend
    session = _open(store)
    assert session == UploadSession(
        id=session.id, user_id="alice", repository="cogs/a", source_id="backing", upstream_location=LOCATION
    )
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") == session
    assert store.get_upload(session.id, user_id="bob", repository="cogs/a") is None
    assert store.get_upload(session.id, user_id="alice", repository="cogs/b") is None
    assert store.get_upload("up-unknown", user_id="alice", repository="cogs/a") is None


def test_an_upload_advances_only_from_where_it_was(backend):
    store, _clock = backend
    session = _open(store)
    moved = "https://backing.internal/v2/cogs/a/blobs/uploads/u1?_state=next"
    assert store.advance_upload(session.id, expected_received=0, received=100, upstream_location=moved) is True
    current = store.get_upload(session.id, user_id="alice", repository="cogs/a")
    assert (current.received, current.upstream_location) == (100, moved)
    # A second writer that still thinks the upload is at 0 loses, and nothing moves.
    assert store.advance_upload(session.id, expected_received=0, received=50, upstream_location=LOCATION) is False
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a").received == 100
    assert store.advance_upload("up-unknown", expected_received=0, received=1, upstream_location=LOCATION) is False
    store.close_upload(session.id)
    store.close_upload(session.id)  # idempotent
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    assert store.advance_upload(session.id, expected_received=100, received=101, upstream_location=LOCATION) is False


def test_an_upload_session_expires(backend):
    store, clock = backend
    session = _open(store)
    clock.advance(UPLOAD_SESSION_TTL_SECONDS - 5)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is not None
    clock.advance(10)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    if isinstance(store, PostgresPublishStore):
        assert store.advance_upload(session.id, expected_received=0, received=1, upstream_location=LOCATION) is False
    # The next open sweeps what expired.
    _open(store, user="bob")
    if isinstance(store, InMemoryPublishStore):
        assert list(store._uploads) != [] and session.id not in store._uploads


def test_opening_past_the_cap_drops_the_users_oldest(backend):
    store, clock = backend
    opened = []
    for _ in range(MAX_UPLOAD_SESSIONS_PER_USER + 2):
        opened.append(_open(store))
        clock.advance(1)
    other = _open(store, user="bob")
    alive = [s.id for s in opened if store.get_upload(s.id, user_id="alice", repository="cogs/a") is not None]
    assert alive == [s.id for s in opened[2:]]
    assert store.get_upload(other.id, user_id="bob", repository="cogs/a") is not None


def test_live_concurrent_claims_have_one_owner_and_concurrent_opens_respect_the_cap():
    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    database = _live_database(max_size=12)
    try:
        _drop_all(database)
        run_collab_schema_migrations(database)
        store = PostgresPublishStore(database)

        def claim(index: int) -> str | None:
            return store.claim_repository(
                "cogs/raced", source_id="backing", owner_org_id=f"org-{index}", created_by=f"user-{index}"
            ).owner_org_id

        with ThreadPoolExecutor(max_workers=12) as pool:
            owners = set(pool.map(claim, range(48)))
        assert len(owners) == 1, "every claimant read the same single owner"
        assert store.get_repository("cogs/raced").owner_org_id in owners

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: _open(store), range(MAX_UPLOAD_SESSIONS_PER_USER + 40)))
        with database.connection() as conn:
            count = conn.execute("SELECT count(*) AS n FROM collab_cog_upload_sessions").fetchone()["n"]
        assert count == MAX_UPLOAD_SESSIONS_PER_USER
    finally:
        _drop_all(database)
        database.close()


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
    def __init__(self, answers=None):
        self.calls: list[tuple[str, tuple]] = []
        self.budgets: list[int] = []
        self.answers = answers or {}

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if text.startswith("SELECT set_config('statement_timeout'"):
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
        self.acquire_timeouts: list = []

    @contextmanager
    def connection(self, timeout=None):
        self.acquire_timeouts.append(timeout)
        yield self.conn


def _fake(answers=None):
    conn = _FakeConnection(answers)
    return PostgresPublishStore(_FakeDb(conn)), conn


REPOSITORY_ROW = {"repository": "cogs/a", "source_id": "backing", "owner_org_id": "org-a", "created_by": "alice"}
UPLOAD_ROW = {
    "id": "up-1",
    "user_id": "alice",
    "repository": "cogs/a",
    "source_id": "backing",
    "upstream_location": LOCATION,
    "received": 7,
}


def test_postgres_claim_inserts_without_overwriting_and_reads_back_in_the_same_transaction():
    store, conn = _fake({"SELECT repository, source_id": [REPOSITORY_ROW]})
    record = store.claim_repository("cogs/a", source_id="backing", owner_org_id="org-b", created_by="bob")
    assert record.owner_org_id == "org-a", "the record that stands, not the one offered"
    insert, read = conn.calls
    assert insert[0].startswith("INSERT INTO collab_cog_repositories")
    assert "ON CONFLICT (repository) DO NOTHING" in insert[0]
    assert insert[1] == ("cogs/a", "backing", "org-b", "bob")
    assert read == (
        "SELECT repository, source_id, owner_org_id, created_by FROM collab_cog_repositories WHERE repository = %s",
        ("cogs/a",),
    )
    assert store.get_repository("cogs/a") == RepositoryRecord("cogs/a", "backing", "org-a", "alice")
    empty, _ = _fake()
    assert empty.get_repository("cogs/a") is None


def test_postgres_published_repositories_are_scoped_to_a_source_and_sorted():
    rows = [{"repository": "b/x"}, {"repository": "a/y"}]
    store, conn = _fake({"FROM collab_cog_repositories WHERE source_id": rows})
    assert store.published_repositories("backing") == ["a/y", "b/x"]
    assert conn.calls == [("SELECT repository FROM collab_cog_repositories WHERE source_id = %s", ("backing",))]


def test_postgres_open_upload_locks_sweeps_inserts_and_caps():
    store, conn = _fake({"INSERT INTO collab_cog_upload_sessions": [{**UPLOAD_ROW, "received": 0}]})
    session = store.open_upload(
        upload_id="up-1", user_id="alice", repository="cogs/a", source_id="backing", upstream_location=LOCATION
    )
    assert session.received == 0 and session.upstream_location == LOCATION
    lock, sweep, insert, cap = conn.calls
    assert lock == ("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, "alice"))
    assert sweep[0] == "DELETE FROM collab_cog_upload_sessions WHERE expires_at <= now()"
    assert insert[1] == ("up-1", "alice", "cogs/a", "backing", LOCATION, UPLOAD_SESSION_TTL_SECONDS)
    assert "ORDER BY created_at DESC, id DESC LIMIT %s" in cap[0]
    assert cap[1] == ("alice", "alice", MAX_UPLOAD_SESSIONS_PER_USER)


def test_postgres_get_upload_matches_owner_repository_and_liveness(monkeypatch):
    from collab_hub_api.cogs import publish_store

    clock = [1000.0]
    monkeypatch.setattr(publish_store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deadline.time, "monotonic", lambda: clock[0])
    store, conn = _fake({"FROM collab_cog_upload_sessions WHERE id": [UPLOAD_ROW]})
    assert store.get_upload("up-1", user_id="alice", repository="cogs/a").received == 7
    ((sql, params),) = conn.calls
    assert "WHERE id = %s AND user_id = %s AND repository = %s AND expires_at > now()" in sql
    assert params == ("up-1", "alice", "cogs/a")
    # A read sweeps expired sessions at most once per interval.
    clock[0] += publish_store.SWEEP_INTERVAL_SECONDS + 1
    store.get_upload("up-1", user_id="alice", repository="cogs/a")
    assert conn.calls[1][0] == "DELETE FROM collab_cog_upload_sessions WHERE expires_at <= now()"
    store.get_upload("up-1", user_id="alice", repository="cogs/a")
    assert len(conn.calls) == 4
    empty, _ = _fake()
    assert empty.get_upload("up-1", user_id="alice", repository="cogs/a") is None


def test_postgres_advance_is_a_compare_and_set_and_close_deletes():
    store, conn = _fake({"UPDATE collab_cog_upload_sessions": [{"id": "up-1"}]})
    assert store.advance_upload("up-1", expected_received=7, received=20, upstream_location=LOCATION) is True
    ((sql, params),) = conn.calls
    assert "WHERE id = %s AND received = %s AND expires_at > now()" in sql
    assert params == (20, LOCATION, "up-1", 7)
    lost, _ = _fake()
    assert lost.advance_upload("up-1", expected_received=7, received=20, upstream_location=LOCATION) is False
    store.close_upload("up-1")
    assert conn.calls[-1] == ("DELETE FROM collab_cog_upload_sessions WHERE id = %s", ("up-1",))


def test_every_postgres_call_is_bounded_by_the_request_budget():
    answers = {"INSERT INTO collab_cog_upload_sessions": [UPLOAD_ROW], "SELECT repository, source_id": [REPOSITORY_ROW]}
    store, conn = _fake(answers)
    calls = (
        lambda: store.get_repository("cogs/a"),
        lambda: store.claim_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u"),
        lambda: store.published_repositories("s"),
        lambda: _open(store, upload_id="up-1"),
        lambda: store.get_upload("up-1", user_id="u", repository="cogs/a"),
        lambda: store.advance_upload("up-1", expected_received=0, received=1, upstream_location=LOCATION),
        lambda: store.close_upload("up-1"),
    )
    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.5)
    try:
        for call in calls:
            call()
        assert len(conn.budgets) == len(conn.calls) and all(1 <= ms <= 500 for ms in conn.budgets)
        assert all(0 < timeout <= 0.5 for timeout in store._db.acquire_timeouts)
        deadline.request_deadline.set(deadline.time.monotonic() - 1)
        for call in calls:
            with pytest.raises(deadline.BudgetExhausted):
                call()
    finally:
        deadline.request_deadline.reset(token)
