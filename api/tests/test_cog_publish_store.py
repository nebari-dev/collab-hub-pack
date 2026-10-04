"""Repository ownership and upload sessions (issue #180): the store contract, on every backend.

In memory always; Postgres against a fake connection always (the statements
and their parameters); Postgres live when ``COLLAB_HUB_TEST_POSTGRES_URL``
names a disposable database -- where the atomic reservation, the per-user cap
and the session lease are actually proven under concurrency.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_api.cogs import deadline
from collab_hub_api.cogs.publish_store import (
    MAX_UPLOAD_ROWS_PER_USER,
    MAX_UPLOAD_SESSIONS_PER_USER,
    RESERVATION_ID_PREFIX,
    UPLOAD_HARD_AGE_SECONDS,
    UPLOAD_ID_PREFIX,
    UPLOAD_OPEN_LOCK_CLASS,
    UPLOAD_SESSION_TTL_SECONDS,
    InMemoryPublishStore,
    PostgresPublishStore,
    PublishStoreUnavailableError,
    RepositoryRecord,
    UnavailablePublishStore,
    UploadLimitError,
    UploadSession,
    new_upload_id,
)
from collab_hub_api.config import Config, build_cog_publish_store
from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
from collab_hub_api.frames.db import PostgresPools

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")
LOCATION = "https://backing.internal/v2/cogs/a/blobs/uploads/u1?_state=secret"
LEASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_upload_ids_are_one_url_safe_segment_and_unguessable():
    ids = {new_upload_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(value.startswith(UPLOAD_ID_PREFIX) and len(value) == len(UPLOAD_ID_PREFIX) + 32 for value in ids)
    assert all(value.replace("-", "").isalnum() for value in ids)


def test_the_unavailable_store_refuses_every_call():
    store = UnavailablePublishStore()
    calls = (
        lambda: store.get_repository("cogs/a"),
        lambda: store.reserve_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u", ttl_seconds=60),
        lambda: store.commit_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u"),
        lambda: store.release_repository("cogs/a", reservation="rsv-1"),
        lambda: store.published_repositories("s"),
        lambda: store.open_upload(upload_id="up-1", user_id="u", repository="cogs/a", source_id="s"),
        lambda: store.attach_upload("up-1", LOCATION),
        lambda: store.get_upload("up-1", user_id="u", repository="cogs/a"),
        lambda: store.lease_upload("up-1", user_id="u", repository="cogs/a", lease_seconds=60),
        lambda: store.advance_upload("up-1", lease=LEASE, expected_received=0, received=1, upstream_location=LOCATION),
        lambda: store.release_upload("up-1", lease=LEASE),
        lambda: store.retire_upload("up-1"),
        lambda: store.close_upload("up-1"),
        lambda: store.claim_stale_uploads(limit=1, lease_seconds=60),
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
    """Moves time for a store: its injected clock in memory, the rows' own timestamps in Postgres."""

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
                " expires_at = expires_at - make_interval(secs => %s),"
                " leased_until = leased_until - make_interval(secs => %s)",
                (seconds, seconds, seconds),
            )
            conn.execute(
                "UPDATE collab_cog_repositories SET reserved_until = reserved_until - make_interval(secs => %s)",
                (seconds,),
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


def _slot(store, user="alice", repository="cogs/a", upload_id=None) -> UploadSession:
    return store.open_upload(
        upload_id=upload_id or new_upload_id(), user_id=user, repository=repository, source_id="backing"
    )


def _open(store, user="alice", repository="cogs/a", upload_id=None, location=LOCATION) -> UploadSession:
    """A slot, with the registry's session attached: what a started upload looks like."""

    session = _slot(store, user, repository, upload_id)
    assert store.attach_upload(session.id, location) is True
    return store.get_upload(session.id, user_id=user, repository=repository)


def _reserve(store, repository="cogs/a", org="org-a", user="alice", source="backing", ttl=300):
    return store.reserve_repository(repository, source_id=source, owner_org_id=org, created_by=user, ttl_seconds=ttl)


def _commit(store, repository="cogs/a", org="org-a", user="alice", source="backing"):
    return store.commit_repository(repository, source_id=source, owner_org_id=org, created_by=user)


# -- repositories: reserved, then owned ----------------------------------------------


def test_a_reservation_is_neither_ownership_nor_enumerated_until_it_is_committed(backend):
    store, _clock = backend
    reserved = _reserve(store)
    assert not reserved.committed and reserved.reservation.startswith(RESERVATION_ID_PREFIX)
    assert (reserved.owner_org_id, reserved.created_by, reserved.source_id) == ("org-a", "alice", "backing")
    assert store.get_repository("cogs/a") is None, "a reservation is not ownership"
    assert store.published_repositories("backing") == [], "and is never enumerated"

    owned = _commit(store)
    assert owned == RepositoryRecord("cogs/a", "backing", "org-a", "alice", committed=True)
    assert store.get_repository("cogs/a") == owned and store.published_repositories("backing") == ["cogs/a"]
    # Committed, it never changes: not by a reservation, a commit or a release, of anyone's.
    assert _reserve(store, org="org-b", user="bob") == owned
    assert _commit(store, org="org-b", user="bob") == owned
    store.release_repository("cogs/a", reservation=reserved.reservation)
    assert store.get_repository("cogs/a") == owned

    # An operator with no organization may own a repository; it then belongs to no organization.
    _reserve(store, "cogs/op", org=None, user="operator")
    assert _commit(store, "cogs/op", org=None, user="operator").owner_org_id is None
    _commit(store, "cogs/b", source="mirror")
    assert store.published_repositories("backing") == ["cogs/a", "cogs/op"]
    assert store.published_repositories("mirror") == ["cogs/b"] and store.published_repositories("none") == []


def test_a_live_reservation_holds_the_name_against_other_organizations_only(backend):
    store, clock = backend
    first = _reserve(store, ttl=300)
    # Another organization is shown that the name is taken, and given nothing to release.
    other = _reserve(store, org="org-b", user="bob")
    assert (other.owner_org_id, other.committed, other.reservation) == ("org-a", False, None)
    # The same organization joins it -- a retry, a colleague publishing at the same moment.
    again = _reserve(store, user="carol")
    assert again.reservation == first.reservation and again.created_by == "carol"
    # A refused publish gives back its own hold only: the name stays held for the one still in flight.
    store.release_repository("cogs/a", reservation=first.reservation)
    assert _reserve(store, org="org-b", user="bob").reservation is None
    store.release_repository("cogs/a", reservation="rsv-not-this-one")
    assert _reserve(store, org="org-b", user="bob").reservation is None
    # The last hold given back frees it.
    store.release_repository("cogs/a", reservation=again.reservation)
    store.release_repository("cogs/a", reservation=again.reservation)  # nothing left to release
    taken = _reserve(store, org="org-b", user="bob")
    assert taken.reservation not in (None, first.reservation) and taken.owner_org_id == "org-b"

    # A reservation whose outcome never came back runs out, and anyone may take the name then.
    clock.advance(301)
    assert store.get_repository("cogs/a") is None and store.published_repositories("backing") == []
    retaken = _reserve(store, org="org-a", user="alice")
    assert retaken.reservation not in (None, taken.reservation) and retaken.owner_org_id == "org-a"
    # The reservation that ran out is not the one that stands: its late release changes nothing.
    store.release_repository("cogs/a", reservation=taken.reservation)
    assert _reserve(store, org="org-b", user="bob").reservation is None
    store.release_repository("cogs/a", reservation=retaken.reservation)
    retaken = _reserve(store, org="org-a", user="alice")
    # The first accepted manifest decides: the commit stands even without a reservation left to turn.
    store.release_repository("cogs/a", reservation=retaken.reservation)
    assert _commit(store, org="org-a").committed is True
    # ... and even over another organization's reservation taken after this one ran out.
    _reserve(store, "cogs/late", org="org-b", user="bob")
    late = _commit(store, "cogs/late", org="org-a")
    assert (late.owner_org_id, late.committed, late.reservation) == ("org-a", True, None)
    assert _commit(store, "cogs/late", org="org-b", user="bob") == late


# -- upload sessions -------------------------------------------------------------------


def test_a_slot_is_taken_before_the_registry_session_and_is_no_upload_until_attached(backend):
    store, _clock = backend
    slot = _slot(store)
    assert slot == UploadSession(
        id=slot.id, user_id="alice", repository="cogs/a", source_id="backing", upstream_location=None
    )
    assert store.get_upload(slot.id, user_id="alice", repository="cogs/a") is None
    assert store.lease_upload(slot.id, user_id="alice", repository="cogs/a", lease_seconds=60) is None
    assert store.attach_upload(slot.id, LOCATION) is True
    session = store.get_upload(slot.id, user_id="alice", repository="cogs/a")
    assert (session.upstream_location, session.received, session.lease) == (LOCATION, 0, None)
    assert store.attach_upload("up-unknown", LOCATION) is False


def test_an_upload_is_found_only_by_its_owner_for_its_repository(backend):
    store, _clock = backend
    session = _open(store)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") == session
    for user, repository, upload_id in (("bob", "cogs/a", session.id), ("alice", "cogs/b", session.id)):
        assert store.get_upload(upload_id, user_id=user, repository=repository) is None
        assert store.lease_upload(upload_id, user_id=user, repository=repository, lease_seconds=60) is None
    assert store.get_upload("up-unknown", user_id="alice", repository="cogs/a") is None


def test_one_holder_at_a_time_may_write_to_a_session(backend):
    store, clock = backend

    def lease():
        return store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=120)

    session = _open(store)
    held = lease()
    assert held.lease is not None and held.received == 0
    assert lease() is None, "a second request, on any replica, is refused while the first holds it"
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is not None, "reads are not blocked"

    # Advancing is the holder's alone, from where the session was, and gives the lease back.
    moved = "https://backing.internal/v2/cogs/a/blobs/uploads/u1?_state=next"
    stranger = held.lease + timedelta(seconds=1)
    advance = store.advance_upload
    assert advance(session.id, lease=stranger, expected_received=0, received=9, upstream_location=LOCATION) is False
    assert advance(session.id, lease=held.lease, expected_received=5, received=9, upstream_location=LOCATION) is False
    assert advance(session.id, lease=held.lease, expected_received=0, received=100, upstream_location=moved) is True
    current = store.get_upload(session.id, user_id="alice", repository="cogs/a")
    assert (current.received, current.upstream_location, current.lease) == (100, moved, None)
    assert advance(session.id, lease=held.lease, expected_received=100, received=200, upstream_location=moved) is False

    # Released without advancing (the registry refused the chunk): the next request may take it.
    second = lease()
    store.release_upload(session.id, lease=stranger)  # not the holder's: nothing happens
    assert lease() is None
    store.release_upload(session.id, lease=second.lease)
    store.release_upload(session.id, lease=second.lease)  # idempotent
    third = lease()
    assert third is not None and third.received == 100

    # A holder that died does not strand the session: the lease runs out.
    clock.advance(121)
    assert lease() is not None

    store.close_upload(session.id)
    store.close_upload(session.id)  # idempotent
    assert lease() is None and store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    assert advance("up-unknown", lease=LEASE, expected_received=0, received=1, upstream_location=LOCATION) is False


def test_an_expired_session_is_unusable_and_kept_until_its_registry_session_is_cancelled(backend):
    store, clock = backend
    session = _open(store)
    held = store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=30)
    clock.advance(UPLOAD_SESSION_TTL_SECONDS - 5)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is not None
    assert store.claim_stale_uploads(limit=5, lease_seconds=60) == []
    clock.advance(10)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    assert store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=30) is None
    # A chunk that was in flight when the session expired cannot move it, on either backend.
    assert (
        store.advance_upload(
            session.id, lease=held.lease, expected_received=0, received=1, upstream_location=LOCATION
        )
        is False
    )

    # Opening another session deletes nothing: the row is what remembers where to cancel.
    _open(store, user="bob")
    (stale,) = store.claim_stale_uploads(limit=5, lease_seconds=60)
    assert (stale.id, stale.upstream_location) == (session.id, LOCATION) and stale.lease is not None
    # Claimed, it is another cleaner's to leave alone -- until that claim runs out (the cancellation failed).
    assert store.claim_stale_uploads(limit=5, lease_seconds=60) == []
    clock.advance(61)
    assert [s.id for s in store.claim_stale_uploads(limit=5, lease_seconds=60)] == [session.id]
    store.close_upload(session.id)
    clock.advance(61)
    assert store.claim_stale_uploads(limit=5, lease_seconds=60) == []


def test_a_session_whose_cancellation_failed_is_retired_not_forgotten(backend):
    store, _clock = backend
    session = _open(store)
    store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=600)
    store.retire_upload(session.id)
    store.retire_upload(session.id)  # idempotent
    store.retire_upload("up-unknown")
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    # Its lease went with it, so the cleanup can take it straight away; and only what was asked for is claimed.
    assert store.claim_stale_uploads(limit=0, lease_seconds=60) == []
    assert store.claim_stale_uploads(limit=5, lease_seconds=60, user_id="bob") == []
    assert [s.id for s in store.claim_stale_uploads(limit=5, lease_seconds=60, user_id="alice")] == [session.id]


def test_opening_past_the_cap_retires_the_users_oldest_and_refuses_when_nothing_is_cleaned_up(backend):
    store, clock = backend
    opened = []
    for _ in range(MAX_UPLOAD_SESSIONS_PER_USER + 2):
        opened.append(_open(store))
        clock.advance(1)
    other = _open(store, user="bob")
    alive = [s.id for s in opened if store.get_upload(s.id, user_id="alice", repository="cogs/a") is not None]
    assert alive == [s.id for s in opened[2:]], "never more live sessions than the cap"
    assert store.get_upload(other.id, user_id="bob", repository="cogs/a") is not None
    # The two that were pushed out are retired, oldest first, with their registry locations intact.
    stale = store.claim_stale_uploads(limit=10, lease_seconds=60)
    assert [s.id for s in stale] == [s.id for s in opened[:2]]
    assert all(s.upstream_location == LOCATION for s in stale)

    # Nobody cancels them at the registry: the rows pile up to the hard limit, and then opening is refused.
    for _ in range(MAX_UPLOAD_ROWS_PER_USER - (MAX_UPLOAD_SESSIONS_PER_USER + 2)):
        _open(store)
    with pytest.raises(UploadLimitError):
        _slot(store)
    assert _slot(store, user="bob") is not None, "the limit is per user"
    # One cleaned up, one slot free.
    store.close_upload(stale[0].id)
    assert _slot(store) is not None

    # Past the hard age limit a row is dropped whether or not its registry session could be cancelled.
    clock.advance(UPLOAD_HARD_AGE_SECONDS + 1)
    assert store.claim_stale_uploads(limit=1000, lease_seconds=60) == []
    assert _slot(store) is not None


def test_live_concurrent_reservations_opens_and_leases():
    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    database = _live_database(max_size=12)
    try:
        _drop_all(database)
        run_collab_schema_migrations(database)
        store = PostgresPublishStore(database)

        def reserve(index: int):
            return _reserve(store, "cogs/raced", org=f"org-{index}", user=f"user-{index}")

        with ThreadPoolExecutor(max_workers=12) as pool:
            records = list(pool.map(reserve, range(48)))
        holders = [record for record in records if record.reservation is not None]
        assert len(holders) == 1, "exactly one organization holds a new name"
        assert {record.owner_org_id for record in records} == {holders[0].owner_org_id}

        # One organization's publishes share a reservation, and it is freed by the last release, not the first.
        with ThreadPoolExecutor(max_workers=12) as pool:
            shared = {record.reservation for record in pool.map(lambda _i: _reserve(store, "cogs/shared"), range(24))}
        assert len(shared) == 1 and None not in shared
        (reservation,) = shared
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: store.release_repository("cogs/shared", reservation=reservation), range(23)))
        assert _reserve(store, "cogs/shared", org="org-b", user="bob").reservation is None
        store.release_repository("cogs/shared", reservation=reservation)
        assert _reserve(store, "cogs/shared", org="org-b", user="bob").owner_org_id == "org-b"

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: _open(store), range(MAX_UPLOAD_SESSIONS_PER_USER + 40)))
        with database.connection() as conn:
            live = conn.execute(
                "SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE expires_at > now()"
            ).fetchone()["n"]
            rows = conn.execute("SELECT count(*) AS n FROM collab_cog_upload_sessions").fetchone()["n"]
        assert live == MAX_UPLOAD_SESSIONS_PER_USER and rows == MAX_UPLOAD_SESSIONS_PER_USER + 40

        session = _open(store, user="bob")
        with ThreadPoolExecutor(max_workers=12) as pool:
            leases = list(
                pool.map(
                    lambda _i: store.lease_upload(session.id, user_id="bob", repository="cogs/a", lease_seconds=60),
                    range(24),
                )
            )
        assert len([lease for lease in leases if lease is not None]) == 1, "one mutation at a time, across connections"

        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda _i: store.claim_stale_uploads(limit=8, lease_seconds=60), range(12)))
        claimed = [s.id for batch in claims for s in batch]
        assert len(claimed) == len(set(claimed)) == 40, "every stale session is claimed by exactly one cleaner"
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


REPOSITORY_ROW = {
    "repository": "cogs/a",
    "source_id": "backing",
    "owner_org_id": "org-a",
    "created_by": "alice",
    "committed": True,
    "reservation": None,
}
UPLOAD_ROW = {
    "id": "up-1",
    "user_id": "alice",
    "repository": "cogs/a",
    "source_id": "backing",
    "upstream_location": LOCATION,
    "received": 7,
    "leased_until": None,
}
READ_REPOSITORY = (
    "SELECT repository, source_id, owner_org_id, created_by, committed, reservation"
    " FROM collab_cog_repositories WHERE repository = %s"
)


def test_postgres_reserve_is_one_conditional_upsert_read_back_in_the_same_transaction():
    store, conn = _fake({"SELECT repository, source_id": [REPOSITORY_ROW]})
    record = _reserve(store, org="org-b", user="bob")
    assert record == RepositoryRecord("cogs/a", "backing", "org-a", "alice"), "the record that stands"
    insert, read = conn.calls
    assert insert[0].startswith("INSERT INTO collab_cog_repositories")
    # A committed row, and a live reservation of another organization's, are left as they are.
    assert "ON CONFLICT (repository) DO UPDATE SET" in insert[0]
    assert "WHERE NOT collab_cog_repositories.committed" in insert[0]
    assert "AND (collab_cog_repositories.reserved_until <= now()" in insert[0]
    assert "OR collab_cog_repositories.owner_org_id IS NOT DISTINCT FROM EXCLUDED.owner_org_id)" in insert[0]
    # A live reservation is joined (one more holder, the same id); an expired one is taken afresh.
    assert (
        "reservation = CASE WHEN collab_cog_repositories.reserved_until > now()"
        " THEN collab_cog_repositories.reservation ELSE EXCLUDED.reservation END" in insert[0]
    )
    assert (
        "holders = CASE WHEN collab_cog_repositories.reserved_until > now()"
        " THEN collab_cog_repositories.holders + 1 ELSE 1 END" in insert[0]
    )
    assert insert[1][:4] == ("cogs/a", "backing", "org-b", "bob") and insert[1][5] == 300
    assert insert[1][4].startswith(RESERVATION_ID_PREFIX)
    assert read == (READ_REPOSITORY, ("cogs/a",))

    # The reservation id is shown to the organization that holds it, and to nobody else.
    reserved = {**REPOSITORY_ROW, "committed": False, "reservation": "rsv-1"}
    holding, _ = _fake({"SELECT repository, source_id": [reserved]})
    assert _reserve(holding, org="org-a").reservation == "rsv-1"
    assert _reserve(holding, org="org-b").reservation is None
    assert _reserve(holding, org=None).reservation is None


def test_postgres_commit_release_and_reads_of_repositories():
    store, conn = _fake({"SELECT repository, source_id": [REPOSITORY_ROW]})
    assert _commit(store) == RepositoryRecord("cogs/a", "backing", "org-a", "alice")
    insert, read = conn.calls
    assert "VALUES (%s, %s, %s, %s, true, now())" in insert[0]
    # The first accepted manifest decides; a committed row is never rewritten.
    assert insert[0].endswith("committed = true, reservation = NULL WHERE NOT collab_cog_repositories.committed")
    assert insert[1] == ("cogs/a", "backing", "org-a", "alice") and read == (READ_REPOSITORY, ("cogs/a",))

    store.release_repository("cogs/a", reservation="rsv-1")
    # Counted down under the row's lock, and deleted only when nobody holds it any more.
    assert conn.calls[-2:] == [
        (
            "UPDATE collab_cog_repositories SET holders = holders - 1"
            " WHERE repository = %s AND NOT committed AND reservation = %s",
            ("cogs/a", "rsv-1"),
        ),
        (
            "DELETE FROM collab_cog_repositories"
            " WHERE repository = %s AND NOT committed AND reservation = %s AND holders <= 0",
            ("cogs/a", "rsv-1"),
        ),
    ]
    assert store.get_repository("cogs/a") == RepositoryRecord("cogs/a", "backing", "org-a", "alice")
    assert conn.calls[-1] == (READ_REPOSITORY + " AND committed", ("cogs/a",))
    empty, _ = _fake()
    assert empty.get_repository("cogs/a") is None


def test_postgres_published_repositories_are_committed_scoped_to_a_source_and_sorted():
    rows = [{"repository": "b/x"}, {"repository": "a/y"}]
    store, conn = _fake({"FROM collab_cog_repositories WHERE source_id": rows})
    assert store.published_repositories("backing") == ["a/y", "b/x"]
    assert conn.calls == [
        ("SELECT repository FROM collab_cog_repositories WHERE source_id = %s AND committed", ("backing",))
    ]


def test_postgres_open_upload_locks_counts_retires_and_inserts_a_slot():
    slot = {**UPLOAD_ROW, "received": 0, "upstream_location": None}
    store, conn = _fake({"INSERT INTO collab_cog_upload_sessions": [slot], "SELECT count(*)": [{"n": 3}]})
    session = _slot(store, upload_id="up-1")
    assert session.received == 0 and session.upstream_location is None
    lock, count, retire, insert = conn.calls
    assert lock == ("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, "alice"))
    assert count == ("SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE user_id = %s", ("alice",))
    # Retired, never deleted: the row is what remembers where to cancel.
    assert retire[0].startswith("UPDATE collab_cog_upload_sessions SET expires_at = now()")
    assert "ORDER BY created_at DESC, id DESC LIMIT %s" in retire[0]
    assert retire[1] == ("alice", "alice", MAX_UPLOAD_SESSIONS_PER_USER - 1)
    assert "(id, user_id, repository, source_id, expires_at)" in insert[0], "no registry location yet"
    assert insert[1] == ("up-1", "alice", "cogs/a", "backing", UPLOAD_SESSION_TTL_SECONDS)
    assert not any(sql.startswith("DELETE") for sql, _ in conn.calls)

    full, conn = _fake({"SELECT count(*)": [{"n": MAX_UPLOAD_ROWS_PER_USER}]})
    with pytest.raises(UploadLimitError):
        _slot(full)
    assert len(conn.calls) == 2, "refused before anything is retired or inserted"


def test_postgres_attach_get_and_lease():
    store, conn = _fake(
        {
            "SET upstream_location": [{"id": "up-1"}],
            "SELECT id, user_id": [UPLOAD_ROW],
            "SET leased_until = clock_timestamp()": [{**UPLOAD_ROW, "leased_until": LEASE}],
        }
    )
    assert store.attach_upload("up-1", LOCATION) is True
    assert conn.calls[-1] == (
        "UPDATE collab_cog_upload_sessions SET upstream_location = %s WHERE id = %s RETURNING id",
        (LOCATION, "up-1"),
    )
    assert store.get_upload("up-1", user_id="alice", repository="cogs/a").received == 7
    sql, params = conn.calls[-1]
    assert "WHERE id = %s AND user_id = %s AND repository = %s AND expires_at > now()" in sql
    assert "AND upstream_location IS NOT NULL" in sql and params == ("up-1", "alice", "cogs/a")

    held = store.lease_upload("up-1", user_id="alice", repository="cogs/a", lease_seconds=120)
    assert held.lease == LEASE
    sql, params = conn.calls[-1]
    # A compare-and-set on the row: taken only if nobody holds it, on a session that is live and attached.
    assert "AND (leased_until IS NULL OR leased_until <= now())" in sql
    assert "AND expires_at > now() AND upstream_location IS NOT NULL" in sql
    assert params == (120, "up-1", "alice", "cogs/a")

    empty, _ = _fake()
    assert empty.attach_upload("up-1", LOCATION) is False
    assert empty.get_upload("up-1", user_id="alice", repository="cogs/a") is None
    assert empty.lease_upload("up-1", user_id="alice", repository="cogs/a", lease_seconds=1) is None


def test_postgres_advance_release_retire_and_close():
    store, conn = _fake({"SET received": [{"id": "up-1"}]})
    assert (
        store.advance_upload("up-1", lease=LEASE, expected_received=7, received=20, upstream_location=LOCATION) is True
    )
    ((sql, params),) = conn.calls
    # The holder's lease, the position it read, and a session that has not expired -- or nothing moves.
    assert "SET received = %s, upstream_location = %s, leased_until = NULL" in sql
    assert "WHERE id = %s AND leased_until = %s AND received = %s AND expires_at > now()" in sql
    assert params == (20, LOCATION, "up-1", LEASE, 7)
    lost, _ = _fake()
    assert (
        lost.advance_upload("up-1", lease=LEASE, expected_received=7, received=20, upstream_location=LOCATION) is False
    )
    store.release_upload("up-1", lease=LEASE)
    assert conn.calls[-1] == (
        "UPDATE collab_cog_upload_sessions SET leased_until = NULL WHERE id = %s AND leased_until = %s",
        ("up-1", LEASE),
    )
    store.retire_upload("up-1")
    assert conn.calls[-1] == (
        "UPDATE collab_cog_upload_sessions SET expires_at = LEAST(expires_at, now()), leased_until = NULL"
        " WHERE id = %s",
        ("up-1",),
    )
    store.close_upload("up-1")
    assert conn.calls[-1] == ("DELETE FROM collab_cog_upload_sessions WHERE id = %s", ("up-1",))


def test_postgres_claim_stale_drops_the_very_old_and_leases_the_rest():
    store, conn = _fake({"SET leased_until = clock_timestamp()": [{**UPLOAD_ROW, "leased_until": LEASE}]})
    (claimed,) = store.claim_stale_uploads(limit=4, lease_seconds=60, user_id="alice")
    assert (claimed.id, claimed.upstream_location, claimed.lease) == ("up-1", LOCATION, LEASE)
    purge, claim = conn.calls
    assert purge == (
        "DELETE FROM collab_cog_upload_sessions WHERE created_at <= now() - make_interval(secs => %s)",
        (UPLOAD_HARD_AGE_SECONDS,),
    )
    assert "WHERE expires_at <= now() AND (leased_until IS NULL OR leased_until <= now())" in claim[0]
    assert "ORDER BY expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED" in claim[0]
    assert claim[1] == (60, "alice", "alice", 4)
    store.claim_stale_uploads(limit=-1, lease_seconds=60)
    assert conn.calls[-1][1] == (60, None, None, 0)


def test_every_postgres_call_is_bounded_by_the_request_budget():
    answers = {
        "INSERT INTO collab_cog_upload_sessions": [UPLOAD_ROW],
        "SELECT repository, source_id": [REPOSITORY_ROW],
        "SELECT count(*)": [{"n": 0}],
    }
    store, conn = _fake(answers)
    calls = (
        lambda: store.get_repository("cogs/a"),
        lambda: _reserve(store),
        lambda: _commit(store),
        lambda: store.release_repository("cogs/a", reservation="rsv-1"),
        lambda: store.published_repositories("s"),
        lambda: _slot(store, upload_id="up-1"),
        lambda: store.attach_upload("up-1", LOCATION),
        lambda: store.get_upload("up-1", user_id="u", repository="cogs/a"),
        lambda: store.lease_upload("up-1", user_id="u", repository="cogs/a", lease_seconds=60),
        lambda: store.advance_upload("up-1", lease=LEASE, expected_received=0, received=1, upstream_location=LOCATION),
        lambda: store.release_upload("up-1", lease=LEASE),
        lambda: store.retire_upload("up-1"),
        lambda: store.close_upload("up-1"),
        lambda: store.claim_stale_uploads(limit=1, lease_seconds=60),
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
