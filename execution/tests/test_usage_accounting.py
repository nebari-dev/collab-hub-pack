"""Executor-independent accounting, including absent and malformed reports."""

import httpx
import pytest

from collab_hub_execution import (
    DurableWorkflowEngine,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    PauseRequest,
    ResultEnvelope,
    RunBudget,
    RunState,
)
from collab_hub_execution.kubernetes import _KubernetesWorker
from collab_hub_execution.orchestration import _NO_SIGNAL


class LocalExecutor:
    """A second executor implementing only the declared worker contract."""

    def __init__(self, worker):
        self.worker = worker
        self.torn_down = 0

    def materialize(self, cog, run_id, instance=""):
        return self.worker

    def teardown(self, worker):
        self.torn_down += 1


def op(run_id="accounting", count=10):
    return OpDefinition(run_id, tuple(OpStep(str(i), "c", "run", "draft") for i in range(count)))


@pytest.mark.parametrize("transport", ["local", "http"])
def test_second_executor_and_http_stop_ten_step_run_at_fifteen_tokens(transport):
    calls = []

    class Worker:
        def interact(self, entry_point, input=None, idempotency_key=None):
            calls.append(input)
            return ResultEnvelope.success("result", usage={"tokens": 10})

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"envelope": 1, "ok": True, "payload": "result", "usage": {"tokens": 10}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        worker = Worker() if transport == "local" else _KubernetesWorker("c", "w", "http://worker", client)
        executor = LocalExecutor(worker)
        track = InMemoryTrackStore()
        engine = DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_tokens=15))
        assert engine.submit(op()) is RunState.BUDGET_EXCEEDED
        assert len(calls) == executor.torn_down == 2
        assert sum(e.payload["usage"]["tokens"] for e in track.replay("accounting")
                   if e.event_type == "interaction_usage") == 20


@pytest.mark.parametrize("usage", [
    None, {}, {"cost": 0}, {"tokens": None}, {"tokens": "10"}, {"tokens": True},
    {"tokens": -1}, {"tokens": 1.5}, {"tokens": float("nan")}, {"tokens": float("inf")}, "unknown",
])
def test_missing_or_invalid_tokens_fail_durably_without_starting_next_step(usage):
    executor = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, usage=usage)})
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_tokens=15))
    assert engine.submit(op()) is RunState.FAILED
    events = track.replay("accounting")
    assert events[-1].payload["error"] == "UsageUnavailable"
    assert events[-1].payload["reason"]
    assert len(executor.materialized) == len(executor.torn_down) == 1
    # A restart cannot turn the unknown spending into a fresh zero balance.
    restarted = DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_tokens=15))
    assert restarted.retry("accounting") is RunState.FAILED
    assert len(executor.materialized) == 1


@pytest.mark.parametrize("usage", [
    None, {"tokens": 0}, {"cost": -1}, {"cost": "0.1"}, {"cost": True},
    {"cost": float("nan")}, {"cost": float("inf")}, {"cost": float("-inf")}, {"cost": 10**400},
])
def test_cost_budget_requires_a_valid_cost_report(usage):
    executor = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, usage=usage)})
    engine = DurableWorkflowEngine(executor=executor, track=InMemoryTrackStore(), budget=RunBudget(max_cost=1))
    assert engine.submit(op()) is RunState.FAILED
    assert len(executor.materialized) == 1


@pytest.mark.parametrize("usage,budget", [
    ({"tokens": 0}, RunBudget(max_tokens=1)),
    ({"cost": 0}, RunBudget(max_cost=1)),
    ({"tokens": 0, "cost": 0.0}, RunBudget(max_tokens=1, max_cost=1)),
    (None, None),
    (None, RunBudget()),
])
def test_explicit_zero_and_unbudgeted_unknown_usage_are_supported(usage, budget):
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, usage=usage)}),
        track=InMemoryTrackStore(), budget=budget,
    )
    assert engine.submit(op(count=2)) is RunState.COMPLETED


@pytest.mark.parametrize("body", [
    {"output": {"usage": {"tokens": 10}}},  # the pre-envelope convention
    {"output": "done"},
    {"output": "done", "usage": {"total_tokens": 10}},
    {"payload": "done", "usage": {"tokens": 10}},  # no envelope version
    {"envelope": 2, "ok": True, "payload": "done", "usage": {"tokens": 10}},  # a version this hub does not read
    {"envelope": 1, "ok": True, "payload": {"usage": {"tokens": 10}}},  # usage hidden in the payload
    {"envelope": 1, "ok": True, "payload": "done", "usage": {"total_tokens": 10}},
    [],
])
def test_wrong_http_shape_or_misplaced_usage_cannot_disable_budget(body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        executor = LocalExecutor(_KubernetesWorker("c", "w", "http://worker", client))
        engine = DurableWorkflowEngine(executor=executor, track=InMemoryTrackStore(), budget=RunBudget(max_tokens=15))
        assert engine.submit(op()) is RunState.FAILED
        assert executor.torn_down == 1


def test_usage_inside_the_payload_is_output_not_accounting():
    payload = {"usage": {"tokens": 1000}, "answer": "unrelated field"}
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(payload, usage={"tokens": 1})}),
        track=track, budget=RunBudget(max_tokens=15),
    )
    assert engine.submit(op(count=2)) is RunState.COMPLETED
    completed = [e for e in track.replay("accounting") if e.event_type == "step_completed"]
    assert all(e.payload["output"] == payload for e in completed)
    assert all(e.payload["usage"] == {"tokens": 1} for e in completed)


def test_worker_must_return_declared_result_type():
    class Worker:
        def interact(self, entry_point, input=None, idempotency_key=None):
            return {"output": "done", "usage": {"tokens": 1}}

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=LocalExecutor(Worker()), track=track)
    assert engine.submit(op()) is RunState.FAILED
    failed = track.replay("accounting")[-1].payload
    assert failed["error"] == "EnvelopeInvalid"
    assert failed["reason"] == "interact() must return a ResultEnvelope"


def test_pause_usage_and_completed_usage_survive_restart_without_double_counting():
    def handler(entry, value, *, signal=_NO_SIGNAL):
        if signal is _NO_SIGNAL:
            raise PauseRequest("feedback", usage={"tokens": 6})
        return ResultEnvelope.success(value, usage={"tokens": 6})

    executor = InMemoryCogExecutor({"c": handler})
    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_tokens=15))

    assert engine().submit(op(count=2)) is RunState.WAITING_AT_GATE
    # 6 (pause) + 6 (resume) + 6 (next step's pause) exceeds 15.
    assert engine().signal("accounting", "go") is RunState.BUDGET_EXCEEDED
    assert engine()._budget_tracker(track.replay("accounting")).tokens == 18
    assert len(executor.materialized) == 3


def test_pause_without_usage_fails_when_spending_is_bounded():
    def handler(entry, value):
        raise PauseRequest("feedback")

    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": handler}), track=InMemoryTrackStore(), budget=RunBudget(max_tokens=15),
    )
    assert engine.submit(op()) is RunState.FAILED


def test_failed_interaction_does_not_reset_unknown_usage_on_retry():
    calls = []

    def handler(entry, value):
        calls.append(value)
        raise httpx.ReadTimeout("response lost")

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": handler}), track=track, budget=RunBudget(max_tokens=15),
    )
    assert engine.submit(op()) is RunState.FAILED
    assert engine.retry("accounting") is RunState.FAILED
    assert calls == ["draft"]
    assert track.replay("accounting")[-1].payload["error"] == "UsageUnavailable"


def test_accounting_survives_teardown_failure_and_retry():
    class Worker:
        def interact(self, entry_point, input=None, idempotency_key=None):
            return ResultEnvelope.success("done", usage={"cost": 0.6})

    class Executor(LocalExecutor):
        def teardown(self, worker):
            super().teardown(worker)
            if self.torn_down == 1:
                raise RuntimeError("cleanup failed")

    executor = Executor(Worker())
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_cost=1))
    assert engine.submit(op()) is RunState.FAILED
    assert engine.retry("accounting") is RunState.BUDGET_EXCEEDED
    assert executor.torn_down == 2
