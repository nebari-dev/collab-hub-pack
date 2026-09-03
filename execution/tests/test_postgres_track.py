"""PostgreSQL Track adapter tests against a real database.

Skipped unless ``TEST_POSTGRES_URL`` is set (the pack's convention for PG-backed
tests). Covers schema creation, append/replay ordering, jsonb round-trip, status
derivation, duplicate event-id rejection, and the one-submission-per-run guard
(#4) that stops two API replicas from both starting the same run.
"""

import os

import pytest

from collab_hub_execution import (
    PostgresTrackStore,
    RunStatus,
    TrackEvent,
    derive_run_status,
)

TEST_PG = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run Postgres adapter tests")


@pytest.fixture
def store():
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(TEST_PG, min_size=1, open=True)
    with pool.connection() as conn:
        conn.execute("DROP INDEX IF EXISTS collab_track_one_submission")
        conn.execute("DROP TABLE IF EXISTS collab_track_events")
        conn.execute("DROP SEQUENCE IF EXISTS collab_track_event_sequence")
        PostgresTrackStore.ensure_schema(conn)
    yield PostgresTrackStore(pool)
    pool.close()


def test_append_replay_ordering_jsonb_and_status(store):
    store.append(TrackEvent(run_id="r", event_type="submitted"))
    store.append(TrackEvent(run_id="r", event_type="op_submitted", payload={"op": {"steps": 2}}))
    store.append(TrackEvent(run_id="other", event_type="submitted"))
    store.append(TrackEvent(run_id="r", event_type="completed"))

    events = store.replay("r")
    assert [e.event_type for e in events] == ["submitted", "op_submitted", "completed"]
    assert [e.sequence for e in events] == sorted(e.sequence for e in events)  # stable global order
    assert events[1].payload == {"op": {"steps": 2}}  # jsonb round-trip
    assert derive_run_status(events) is RunStatus.COMPLETED
    assert store.replay("r", after_sequence=events[0].sequence)[0].event_type == "op_submitted"


def test_duplicate_event_id_is_rejected(store):
    first = store.append(TrackEvent(run_id="r", event_type="x"))
    with pytest.raises(Exception):  # UNIQUE(event_id) violation
        store.append(TrackEvent(run_id="r", event_type="x", event_id=first.event_id))


def test_only_one_submission_per_run(store):
    store.append(TrackEvent(run_id="r", event_type="op_submitted"))
    with pytest.raises(Exception):  # partial unique index blocks a second op_submitted (#4 guard)
        store.append(TrackEvent(run_id="r", event_type="op_submitted"))
