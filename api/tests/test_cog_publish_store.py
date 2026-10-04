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
    PENDING_ENUMERATION_GRACE_SECONDS,
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
OPENING = 120


def test_upload_ids_are_one_url_safe_segment_and_unguessable():
    ids = {new_upload_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(value.startswith(UPLOAD_ID_PREFIX) and len(value) == len(UPLOAD_ID_PREFIX) + 32 for value in ids)
    assert all(value.replace("-", "").isalnum() for value in ids)


def test_the_unavailable_store_refuses_every_call():
    store = UnavailablePublishStore()
    calls = (
        lambda: store.get_repository("cogs/a"),
        lambda: store.reserve_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u"),
        lambda: store.commit_repository("cogs/a", source_id="s", owner_org_id="o", created_by="u"),
        lambda: store.release_repository("cogs/a", owner_org_id="o"),
        lambda: store.published_repositories("s"),
        lambda: store.commit_found("s", ["cogs/a"]),
        lambda: store.open_upload(upload_id="up-1", user_id="u", repository="cogs/a", source_id="s", lease_seconds=1),
        lambda: store.attach_upload("up-1", lease=LEASE, upstream_location=LOCATION),
        lambda: store.record_orphan(
            upload_id="up-1", user_id="u", repository="cogs/a", source_id="s", upstream_location=LOCATION
        ),
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

    def advance(self, seconds: float, *, leases: bool = True) -> None:
        """``leases=False`` for a short step under a lease whose holder still has to present it afterwards:
        in Postgres time is moved by rewriting timestamps, and a rewritten lease is no longer the holder's."""

        if self.database is None:
            self.now += timedelta(seconds=seconds)
            return
        with self.database.connection() as conn:
            conn.execute(
                "UPDATE collab_cog_upload_sessions SET created_at = created_at - make_interval(secs => %s),"
                " expires_at = expires_at - make_interval(secs => %s),"
                " leased_until = leased_until - make_interval(secs => %s)",
                (seconds, seconds, seconds if leases else 0),
            )
            conn.execute(
                "UPDATE collab_cog_repositories SET created_at = created_at - make_interval(secs => %s)",
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
        upload_id=upload_id or new_upload_id(),
        user_id=user,
        repository=repository,
        source_id="backing",
        lease_seconds=OPENING,
    )


def _open(store, user="alice", repository="cogs/a", upload_id=None, location=LOCATION) -> UploadSession:
    """A slot, with the registry's session attached: what a started upload looks like."""

    slot = _slot(store, user, repository, upload_id)
    assert store.attach_upload(slot.id, lease=slot.lease, upstream_location=location) is True
    return store.get_upload(slot.id, user_id=user, repository=repository)


def _reserve(store, repository="cogs/a", org="org-a", user="alice", source="backing"):
    return store.reserve_repository(repository, source_id=source, owner_org_id=org, created_by=user)


def _commit(store, repository="cogs/a", org="org-a", user="alice", source="backing"):
    return store.commit_repository(repository, source_id=source, owner_org_id=org, created_by=user)


# -- repositories: pending, then committed; never reassigned -----------------------------


def test_a_repository_is_its_publishers_before_the_manifest_is_forwarded_and_committed_after(backend):
    store, _clock = backend
    assert store.get_repository("cogs/a") is None
    pending = _reserve(store)
    assert pending == RepositoryRecord("cogs/a", "backing", "org-a", "alice", committed=False)
    assert store.get_repository("cogs/a") == pending, "already this organization's, though not yet settled"
    assert store.published_repositories("backing") == [], "and not something a sweep is sent to yet"

    owned = _commit(store)
    assert owned == RepositoryRecord("cogs/a", "backing", "org-a", "alice", committed=True)
    assert store.get_repository("cogs/a") == owned and store.published_repositories("backing") == ["cogs/a"]
    # Committed, it never changes: not by a reservation, a commit or a release, of anyone's.
    assert _reserve(store, org="org-b", user="bob") == owned
    assert _commit(store, org="org-b", user="bob") == owned
    store.release_repository("cogs/a", owner_org_id="org-a")
    store.release_repository("cogs/a", owner_org_id="org-b")
    assert store.get_repository("cogs/a") == owned

    # An operator with no organization may own a repository; it then belongs to no organization.
    _reserve(store, "cogs/op", org=None, user="operator")
    assert _commit(store, "cogs/op", org=None, user="operator").owner_org_id is None
    # A commit with no row left to settle (an operator released it meanwhile) writes one.
    assert _commit(store, "cogs/b", source="mirror") == RepositoryRecord("cogs/b", "mirror", "org-a", "alice")
    assert store.published_repositories("backing") == ["cogs/a", "cogs/op"]
    assert store.published_repositories("mirror") == ["cogs/b"] and store.published_repositories("none") == []


def test_a_pending_repository_is_never_handed_to_another_organization(backend):
    store, clock = backend
    _reserve(store)
    # Another organization is shown whose it is, and nothing of the row changes: not now, and not ever.
    for wait in (0, 3600, 30 * 24 * 3600):
        clock.advance(wait)
        other = _reserve(store, org="org-b", user="bob")
        assert (other.owner_org_id, other.created_by, other.committed) == ("org-a", "alice", False)
        assert _reserve(store, org=None, user="operator").owner_org_id == "org-a"
        # It cannot settle the name either, nor release what it does not hold.
        assert _commit(store, org="org-b", user="bob").owner_org_id == "org-a"
        store.release_repository("cogs/a", owner_org_id="org-b")
        assert store.get_repository("cogs/a") == RepositoryRecord("cogs/a", "backing", "org-a", "alice", False)
    # The organization that holds it settles it whenever its manifest is known to be there.
    assert _commit(store) == RepositoryRecord("cogs/a", "backing", "org-a", "alice", committed=True)


def test_a_pending_repository_is_freed_only_when_every_attempt_in_flight_was_refused(backend):
    store, _clock = backend
    _reserve(store)  # alice's manifest is on its way
    _reserve(store, user="carol")  # and a colleague's, at the same moment
    # Alice's is refused: the name is still held for Carol's.
    store.release_repository("cogs/a", owner_org_id="org-a")
    assert store.get_repository("cogs/a") is not None
    assert _reserve(store, org="org-b", user="bob").owner_org_id == "org-a"
    # Carol's too: nothing of that organization's can be in the registry, and the name is free.
    store.release_repository("cogs/a", owner_org_id="org-a")
    assert store.get_repository("cogs/a") is None
    store.release_repository("cogs/a", owner_org_id="org-a")  # nothing left to release
    assert _reserve(store, org="org-b", user="bob").owner_org_id == "org-b"

    # An attempt whose outcome is unknown is never released, so its refused retry does not free the name.
    _reserve(store, "cogs/lost")  # the registry never answered
    _reserve(store, "cogs/lost")  # the retry
    store.release_repository("cogs/lost", owner_org_id="org-a")  # refused
    assert store.get_repository("cogs/lost") == RepositoryRecord("cogs/lost", "backing", "org-a", "alice", False)


def test_sweeps_are_sent_to_a_pending_repository_after_a_grace_period_and_settle_it(backend):
    store, clock = backend
    _reserve(store)
    _reserve(store, "cogs/empty")
    _commit(store, "cogs/done")
    assert store.published_repositories("backing") == ["cogs/done"]
    clock.advance(PENDING_ENUMERATION_GRACE_SECONDS - 5)
    assert store.published_repositories("backing") == ["cogs/done"], "a publish may still be on its way"
    clock.advance(10)
    assert store.published_repositories("backing") == ["cogs/a", "cogs/done", "cogs/empty"]
    assert store.published_repositories("mirror") == []
    # A sweep found content in two of them; the wrong source and unknown names settle nothing.
    assert store.commit_found("mirror", ["cogs/a"]) == 0 and store.commit_found("backing", []) == 0
    assert store.commit_found("backing", ["cogs/a", "cogs/done", "cogs/unknown", "cogs/a"]) == 1
    assert store.get_repository("cogs/a") == RepositoryRecord("cogs/a", "backing", "org-a", "alice", committed=True)
    assert store.get_repository("cogs/empty").committed is False, "nothing found there: still pending, still owned"


# -- upload sessions -------------------------------------------------------------------


def test_a_slot_is_held_by_its_opener_until_the_registry_session_is_attached(backend):
    store, clock = backend
    slot = _slot(store)
    assert (slot.upstream_location, slot.received) == (None, 0) and slot.lease is not None
    assert store.get_upload(slot.id, user_id="alice", repository="cogs/a") is None
    assert store.lease_upload(slot.id, user_id="alice", repository="cogs/a", lease_seconds=60) is None
    # While it is opening, the cleanup leaves it alone -- whatever else it is asked to take.
    assert store.claim_stale_uploads(limit=10, lease_seconds=60) == []
    assert store.attach_upload(slot.id, lease=slot.lease + timedelta(seconds=1), upstream_location=LOCATION) is False
    assert store.attach_upload(slot.id, lease=slot.lease, upstream_location=LOCATION) is True
    session = store.get_upload(slot.id, user_id="alice", repository="cogs/a")
    assert (session.upstream_location, session.received, session.lease) == (LOCATION, 0, None)
    assert store.attach_upload("up-unknown", lease=LEASE, upstream_location=LOCATION) is False

    # An opener that died: once its lease has run out the slot is dead, cleaned up, and cannot be attached.
    abandoned = _slot(store)
    clock.advance(OPENING + 1)
    (claimed,) = store.claim_stale_uploads(limit=10, lease_seconds=60)
    assert (claimed.id, claimed.upstream_location) == (abandoned.id, None)
    assert store.attach_upload(abandoned.id, lease=abandoned.lease, upstream_location=LOCATION) is False


def test_a_registry_session_with_no_slot_is_remembered_for_cancellation(backend):
    store, _clock = backend
    store.record_orphan(
        upload_id="up-orphan", user_id="alice", repository="cogs/a", source_id="backing", upstream_location=LOCATION
    )
    assert store.get_upload("up-orphan", user_id="alice", repository="cogs/a") is None, "never an upload a client has"
    (claimed,) = store.claim_stale_uploads(limit=10, lease_seconds=60)
    assert (claimed.id, claimed.upstream_location) == ("up-orphan", LOCATION)


def test_an_upload_is_found_only_by_its_owner_for_its_repository(backend):
    store, _clock = backend
    session = _open(store)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") == session
    for user, repository, upload_id in (("bob", "cogs/a", session.id), ("alice", "cogs/b", session.id)):
        assert store.get_upload(upload_id, user_id=user, repository=repository) is None
        assert store.lease_upload(upload_id, user_id=user, repository=repository, lease_seconds=60) is None
    assert store.get_upload("up-unknown", user_id="alice", repository="cogs/a") is None


def test_one_holder_at_a_time_may_write_to_a_session(backend):
    store, _clock = backend

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

    # Given back unchanged (the registry definitely took nothing): the next request may take it.
    second = lease()
    store.release_upload(session.id, lease=stranger)  # not the holder's: nothing happens
    assert lease() is None
    store.release_upload(session.id, lease=second.lease)
    store.release_upload(session.id, lease=second.lease)  # idempotent
    third = lease()
    assert third is not None and third.received == 100
    store.release_upload(session.id, lease=third.lease)

    store.close_upload(session.id)
    store.close_upload(session.id)  # idempotent
    assert lease() is None and store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    assert advance("up-unknown", lease=LEASE, expected_received=0, received=1, upstream_location=LOCATION) is False


def test_a_lease_that_runs_out_kills_the_session_instead_of_passing_to_the_next_request(backend):
    """Its holder forwarded something and never recorded what: the byte count cannot be built on."""

    store, clock = backend
    session = _open(store)
    held = store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=120)
    clock.advance(121)
    assert store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=120) is None
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None, "unknown to its client now"
    # It is the cleanup's: cancelled at the registry, then forgotten. The late holder cannot move it any more.
    (claimed,) = store.claim_stale_uploads(limit=5, lease_seconds=60)
    assert (claimed.id, claimed.upstream_location) == (session.id, LOCATION)
    assert (
        store.advance_upload(session.id, lease=held.lease, expected_received=0, received=8, upstream_location=LOCATION)
        is False
    )
    store.release_upload(session.id, lease=held.lease)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None


def test_an_expired_session_is_unusable_and_kept_until_its_registry_session_is_cancelled(backend):
    store, clock = backend
    session = _open(store)
    clock.advance(UPLOAD_SESSION_TTL_SECONDS - 5)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is not None
    assert store.claim_stale_uploads(limit=5, lease_seconds=60) == []
    held = store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=30)
    clock.advance(10, leases=False)
    assert store.get_upload(session.id, user_id="alice", repository="cogs/a") is None
    assert store.lease_upload(session.id, user_id="alice", repository="cogs/a", lease_seconds=30) is None
    # A chunk that was in flight when the session expired cannot move it, on either backend.
    assert (
        store.advance_upload(session.id, lease=held.lease, expected_received=0, received=1, upstream_location=LOCATION)
        is False
    )
    store.release_upload(session.id, lease=held.lease)

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


def test_the_cap_never_retires_a_slot_that_is_still_opening(backend, monkeypatch):
    """Its registry session is on its way; retired now, nobody would know where to cancel it."""

    from collab_hub_api.cogs import publish_store

    monkeypatch.setattr(publish_store, "MAX_UPLOAD_SESSIONS_PER_USER", 2)
    store, clock = backend
    opening = _slot(store)  # the registry has not answered yet
    clock.advance(1, leases=False)
    first = _open(store)
    clock.advance(1, leases=False)
    second = _open(store)  # over the cap: the oldest *attached* session goes, not the opening slot
    assert store.get_upload(first.id, user_id="alice", repository="cogs/a") is None
    assert store.get_upload(second.id, user_id="alice", repository="cogs/a") is not None
    assert [s.id for s in store.claim_stale_uploads(limit=10, lease_seconds=60)] == [first.id]
    # The registry answers: the slot is still there to take the location.
    assert store.attach_upload(opening.id, lease=opening.lease, upstream_location=LOCATION) is True
    assert store.get_upload(opening.id, user_id="alice", repository="cogs/a") is not None


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
        assert len({record.owner_org_id for record in records}) == 1, "exactly one organization holds a new name"

        # One organization's attempts are counted, and the name is freed by the last refusal, not the first.
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: _reserve(store, "cogs/shared"), range(24)))
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: store.release_repository("cogs/shared", owner_org_id="org-a"), range(23)))
        assert _reserve(store, "cogs/shared", org="org-b", user="bob").owner_org_id == "org-a"
        store.release_repository("cogs/shared", owner_org_id="org-a")
        assert _reserve(store, "cogs/shared", org="org-b", user="bob").owner_org_id == "org-b"

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _i: _open(store), range(MAX_UPLOAD_SESSIONS_PER_USER + 40)))
        with database.connection() as conn:
            live = conn.execute(
                "SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE expires_at > now()"
            ).fetchone()["n"]
            rows = conn.execute("SELECT count(*) AS n FROM collab_cog_upload_sessions").fetchone()["n"]
        assert live <= MAX_UPLOAD_SESSIONS_PER_USER + 12 and rows == MAX_UPLOAD_SESSIONS_PER_USER + 40
        _open(store)
        with database.connection() as conn:
            live = conn.execute(
                "SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE expires_at > now()"
            ).fetchone()["n"]
        assert live == MAX_UPLOAD_SESSIONS_PER_USER, "with nothing still opening, the cap is exact"

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
        assert len(claimed) == len(set(claimed)) == 41, "every stale session is claimed by exactly one cleaner"
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
    "SELECT repository, source_id, owner_org_id, created_by, committed"
    " FROM collab_cog_repositories WHERE repository = %s"
)
OWN_PENDING = (
    "WHERE NOT collab_cog_repositories.committed"
    " AND collab_cog_repositories.owner_org_id IS NOT DISTINCT FROM EXCLUDED.owner_org_id"
)


def test_postgres_reserve_never_changes_whose_a_row_is():
    store, conn = _fake({"SELECT repository, source_id": [REPOSITORY_ROW]})
    record = _reserve(store, org="org-b", user="bob")
    assert record == RepositoryRecord("cogs/a", "backing", "org-a", "alice"), "the record that stands"
    insert, read = conn.calls
    assert insert[0].startswith("INSERT INTO collab_cog_repositories")
    assert "VALUES (%s, %s, %s, %s, false, 1)" in insert[0]
    # On conflict the only thing that can change is the count, and only on this organization's own pending row:
    # nothing about time, and no column that says whose the row is.
    assert f"ON CONFLICT (repository) DO UPDATE SET holders = collab_cog_repositories.holders + 1 {OWN_PENDING}" in (
        insert[0]
    )
    assert "now()" not in insert[0] and "owner_org_id =" not in insert[0]
    assert insert[1] == ("cogs/a", "backing", "org-b", "bob")
    assert read == (READ_REPOSITORY, ("cogs/a",))


def test_postgres_commit_release_and_reads_of_repositories():
    store, conn = _fake({"SELECT repository, source_id": [REPOSITORY_ROW]})
    assert _commit(store) == RepositoryRecord("cogs/a", "backing", "org-a", "alice")
    insert, read = conn.calls
    assert "VALUES (%s, %s, %s, %s, true, 0)" in insert[0]
    # Bound to the organization: a commit settles its own pending row, never another's.
    assert insert[0].endswith(f"ON CONFLICT (repository) DO UPDATE SET committed = true {OWN_PENDING}")
    assert insert[1] == ("cogs/a", "backing", "org-a", "alice") and read == (READ_REPOSITORY, ("cogs/a",))

    store.release_repository("cogs/a", owner_org_id=None)
    # Counted down under the row's lock, and deleted only when no attempt of that organization's is left.
    assert conn.calls[-2:] == [
        (
            "UPDATE collab_cog_repositories SET holders = holders - 1"
            " WHERE repository = %s AND NOT committed AND owner_org_id IS NOT DISTINCT FROM %s",
            ("cogs/a", None),
        ),
        (
            "DELETE FROM collab_cog_repositories"
            " WHERE repository = %s AND NOT committed AND owner_org_id IS NOT DISTINCT FROM %s AND holders <= 0",
            ("cogs/a", None),
        ),
    ]
    assert store.get_repository("cogs/a") == RepositoryRecord("cogs/a", "backing", "org-a", "alice")
    assert conn.calls[-1] == (READ_REPOSITORY, ("cogs/a",))
    empty, _ = _fake()
    assert empty.get_repository("cogs/a") is None


def test_postgres_enumeration_takes_committed_rows_and_pending_ones_past_the_grace_period():
    rows = [{"repository": "b/x"}, {"repository": "a/y"}]
    store, conn = _fake({"FROM collab_cog_repositories WHERE source_id": rows, "SET committed = true": rows})
    assert store.published_repositories("backing") == ["a/y", "b/x"]
    assert conn.calls == [
        (
            "SELECT repository FROM collab_cog_repositories"
            " WHERE source_id = %s AND (committed OR created_at <= now() - make_interval(secs => %s))",
            ("backing", PENDING_ENUMERATION_GRACE_SECONDS),
        )
    ]
    assert store.commit_found("backing", ["b/x", "a/y", "b/x"]) == 2
    assert conn.calls[-1] == (
        "UPDATE collab_cog_repositories SET committed = true"
        " WHERE source_id = %s AND repository = ANY(%s) AND NOT committed RETURNING repository",
        ("backing", ["a/y", "b/x"]),
    )
    assert store.commit_found("backing", []) == 0 and len(conn.calls) == 2, "nothing to ask the database"


def test_postgres_open_upload_locks_counts_retires_and_inserts_a_leased_slot():
    slot = {**UPLOAD_ROW, "received": 0, "upstream_location": None, "leased_until": LEASE}
    store, conn = _fake({"INSERT INTO collab_cog_upload_sessions": [slot], "SELECT count(*)": [{"n": 3}]})
    session = _slot(store, upload_id="up-1")
    assert (session.received, session.upstream_location, session.lease) == (0, None, LEASE)
    lock, count, retire, insert = conn.calls
    assert lock == ("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, "alice"))
    assert count == ("SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE user_id = %s", ("alice",))
    # Retired, never deleted: the row is what remembers where to cancel. And never a slot with no location yet.
    assert retire[0].startswith("UPDATE collab_cog_upload_sessions SET expires_at = now()")
    assert "AND expires_at > now() AND upstream_location IS NOT NULL ORDER BY created_at, id LIMIT GREATEST((" in (
        retire[0]
    )
    assert retire[1] == ("alice", "alice", MAX_UPLOAD_SESSIONS_PER_USER - 1)
    assert "(id, user_id, repository, source_id, leased_until, expires_at)" in insert[0], "no registry location yet"
    assert insert[1] == ("up-1", "alice", "cogs/a", "backing", OPENING, UPLOAD_SESSION_TTL_SECONDS)
    assert not any(sql.startswith("DELETE") for sql, _ in conn.calls)

    full, conn = _fake({"SELECT count(*)": [{"n": MAX_UPLOAD_ROWS_PER_USER}]})
    with pytest.raises(UploadLimitError):
        _slot(full)
    assert len(conn.calls) == 2, "refused before anything is retired or inserted"


def test_postgres_attach_orphan_get_and_lease():
    store, conn = _fake(
        {
            "SET upstream_location": [{"id": "up-1"}],
            "SELECT id, user_id": [UPLOAD_ROW],
            "SET leased_until = clock_timestamp()": [{**UPLOAD_ROW, "leased_until": LEASE}],
        }
    )
    assert store.attach_upload("up-1", lease=LEASE, upstream_location=LOCATION) is True
    assert conn.calls[-1] == (
        "UPDATE collab_cog_upload_sessions SET upstream_location = %s, leased_until = NULL"
        " WHERE id = %s AND leased_until = %s RETURNING id",
        (LOCATION, "up-1", LEASE),
    )
    store.record_orphan(
        upload_id="up-2", user_id="alice", repository="cogs/a", source_id="backing", upstream_location=LOCATION
    )
    sql, params = conn.calls[-1]
    assert "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (id) DO NOTHING" in sql, "dead on arrival: expired at once"
    assert params == ("up-2", "alice", "cogs/a", "backing", LOCATION)

    assert store.get_upload("up-1", user_id="alice", repository="cogs/a").received == 7
    sql, params = conn.calls[-1]
    assert "WHERE id = %s AND user_id = %s AND repository = %s AND expires_at > now()" in sql
    assert "AND upstream_location IS NOT NULL AND (leased_until IS NULL OR leased_until > now())" in sql
    assert params == ("up-1", "alice", "cogs/a")

    held = store.lease_upload("up-1", user_id="alice", repository="cogs/a", lease_seconds=120)
    assert held.lease == LEASE
    sql, params = conn.calls[-1]
    # A compare-and-set on the row, and only a lease that was given back is free: one that ran out is not.
    assert sql.split("RETURNING")[0].rstrip().endswith("AND upstream_location IS NOT NULL AND leased_until IS NULL")
    assert params == (120, "up-1", "alice", "cogs/a")

    empty, _ = _fake()
    assert empty.attach_upload("up-1", lease=LEASE, upstream_location=LOCATION) is False
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


def test_postgres_claim_stale_drops_the_very_old_and_claims_the_dead():
    store, conn = _fake({"SET leased_until = clock_timestamp()": [{**UPLOAD_ROW, "leased_until": LEASE}]})
    (claimed,) = store.claim_stale_uploads(limit=4, lease_seconds=60, user_id="alice")
    assert (claimed.id, claimed.upstream_location, claimed.lease) == ("up-1", LOCATION, LEASE)
    purge, claim = conn.calls
    assert purge == (
        "DELETE FROM collab_cog_upload_sessions WHERE created_at <= now() - make_interval(secs => %s)",
        (UPLOAD_HARD_AGE_SECONDS,),
    )
    # Dead: expired with nobody holding it, or holding a lease that ran out. A live lease is never taken over.
    assert "WHERE ((leased_until IS NULL AND expires_at <= now()) OR leased_until <= now())" in claim[0]
    assert "expires_at = LEAST(expires_at, now())" in claim[0], "claimed, it is dead to its client whatever it was"
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
        lambda: store.release_repository("cogs/a", owner_org_id="org-a"),
        lambda: store.published_repositories("s"),
        lambda: store.commit_found("s", ["cogs/a"]),
        lambda: _slot(store, upload_id="up-1"),
        lambda: store.attach_upload("up-1", lease=LEASE, upstream_location=LOCATION),
        lambda: store.record_orphan(
            upload_id="up-2", user_id="u", repository="cogs/a", source_id="s", upstream_location=LOCATION
        ),
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
