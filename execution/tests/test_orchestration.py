"""Regressions for budget retry and recovery at the submission boundary."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from collab_hub_execution import (
    DurableWorkflowEngine,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    InteractionResult,
    OpDefinition,
    OpStep,
    RunBudget,
    RunStatus,
)


@pytest.mark.parametrize(
    "budget,status",
    [
        (RunBudget(max_duration=timedelta(0)), RunStatus.TIMED_OUT),
        (RunBudget(max_tokens=10), RunStatus.BUDGET_EXCEEDED),
        (RunBudget(max_cost=1), RunStatus.BUDGET_EXCEEDED),
    ],
)
def test_exhausted_budget_retry_is_rejected_without_mutating_run(budget, status):
    calls = []
    executor = InMemoryCogExecutor({
        "c": lambda entry, value: calls.append(value) or InteractionResult(usage={"tokens": 10, "cost": 1}),
    })
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track, budget=budget)
    op = OpDefinition("budget", (OpStep("first", "c", "run"), OpStep("second", "c", "run")))
    assert engine.submit(op) is status
    events, before = track.replay(op.run_id), list(calls)
    restarted = DurableWorkflowEngine(executor=executor, track=track, budget=budget)
    with pytest.raises(ValueError, match="exhausted its budget; start a new run"):
        restarted.retry(op.run_id)
    assert track.replay(op.run_id) == events
    assert calls == before
    assert restarted.observe(op.run_id) is status


class JsonTrack(InMemoryTrackStore):
    """Exercise JSON storage semantics without requiring a database."""

    def append(self, event):
        return super().append(replace(event, payload=json.loads(json.dumps(event.payload))))


def test_recovery_after_submission_roundtrips_input_and_rejects_changed_op():
    class CrashAfterSubmission(JsonTrack):
        def append(self, event):
            stored = super().append(event)
            if event.event_type == "op_submitted":
                raise SystemExit("process stopped after commit")
            return stored

    calls = []
    track = CrashAfterSubmission()
    executor = InMemoryCogExecutor({"c": lambda entry, value: calls.append(value) or value})
    op = OpDefinition("recover", (OpStep("s", "c", "run", {"items": ("a", "b")}),))
    first = DurableWorkflowEngine(executor=executor, track=track)
    with pytest.raises(SystemExit):
        first.submit(op)
    assert first.observe(op.run_id) is RunStatus.SUBMITTED
    assert [e.event_type for e in track.replay(op.run_id)] == ["op_submitted"]
    assert calls == []

    restarted = DurableWorkflowEngine(executor=executor, track=track)
    changed = OpDefinition("recover", (OpStep("s", "c", "run", {"items": ("a", "other")}),))
    with pytest.raises(ValueError, match="different Op"):
        restarted.submit(changed)
    assert restarted.submit(op) is RunStatus.COMPLETED
    assert len(calls) == 1
    assert not any(e.event_type == "submitted" for e in track.replay(op.run_id))
