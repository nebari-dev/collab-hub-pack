from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_execution import (
    BudgetExceeded,
    BudgetTracker,
    InMemoryTrackStore,
    InvalidTransition,
    RunBudget,
    RunState,
    TrackEvent,
    Worker,
    WorkerState,
    derive_run_status,
)


def test_track_replays_in_append_order_and_derives_status():
    store = InMemoryTrackStore()
    for kind in ("submitted", "run_picked_up", "materialized", "ready", "interaction_started", "idle", "completed"):
        store.append(TrackEvent(run_id="run-1", event_type=kind))

    events = store.replay("run-1")
    assert [event.sequence for event in events] == list(range(1, 8))
    assert derive_run_status(events) is RunState.COMPLETED
    assert list(store.stream("run-1")) == list(events)


def test_track_stream_can_replay_after_a_cursor():
    store = InMemoryTrackStore()
    first = store.append(TrackEvent(run_id="run-1", event_type="submitted"))
    store.append(TrackEvent(run_id="run-1", event_type="completed"))

    assert [event.event_type for event in store.stream("run-1", after_sequence=first.sequence or 0)] == ["completed"]


def test_worker_rejects_skipped_transition():
    worker = Worker.materialize("c", step="s").after
    with pytest.raises(InvalidTransition):
        worker.invoke(entry_point="run")

    worker = worker.ready().after
    worker = worker.invoke(entry_point="run").after
    worker = worker.envelope_returned().after
    worker = worker.tear_down(reason="one_shot").after
    worker = worker.torn_down().after
    assert worker.state is WorkerState.TORN_DOWN


def test_budget_rejects_duration_and_usage_overruns():
    started = datetime(2026, 1, 1, tzinfo=UTC)
    tracker = BudgetTracker(RunBudget(max_duration=timedelta(seconds=5), max_tokens=10), started_at=started)
    tracker.consume(tokens=9, now=started + timedelta(seconds=1))
    with pytest.raises(BudgetExceeded):
        tracker.consume(tokens=1, now=started + timedelta(seconds=1))

    expired = BudgetTracker(RunBudget(max_duration=timedelta(seconds=5)), started_at=started)
    with pytest.raises(BudgetExceeded):
        expired.check(now=started + timedelta(seconds=5))
