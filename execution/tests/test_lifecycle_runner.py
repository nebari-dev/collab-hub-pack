"""The lifecycle runner: the lifecycle lives in its step functions and the one driver that runs them."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from collab_hub_execution import (
    STEP_FUNCTIONS,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
    RunState,
)
from collab_hub_execution.runner import LifecycleRunner

SOURCE = Path(__file__).resolve().parents[1] / "src" / "collab_hub_execution"


def test_the_step_functions_are_registered():
    assert list(STEP_FUNCTIONS) == [
        "resolve", "materialize", "interact", "read_envelope", "teardown", "evaluate_gate",
        "complete", "escalate", "fail", "complete_approved", "stop_for_budget",
    ]
    assert all(getattr(LifecycleRunner, name) is function for name, function in STEP_FUNCTIONS.items())


def _spied(runner: LifecycleRunner, calls: list[str]) -> LifecycleRunner:
    for name in STEP_FUNCTIONS:
        bound = getattr(runner, name)

        def spy(*args, _bound=bound, _name=name, **kwargs):
            calls.append(_name)
            return _bound(*args, **kwargs)

        setattr(runner, name, spy)
    return runner


def _engine(handlers, calls, **kwargs) -> LifecycleRunner:
    return _spied(LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=InMemoryTrackStore(),
                                        **kwargs), calls)


def test_a_completed_step_goes_through_the_runner_s_step_functions():
    calls: list[str] = []
    engine = _engine({"c": lambda entry, value: {"ok": value}}, calls)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", 1),))) is RunState.COMPLETED
    assert calls == ["resolve", "materialize", "interact", "read_envelope", "teardown", "evaluate_gate", "complete"]


def test_every_step_function_is_reached_through_the_runner():
    calls: list[str] = []
    # An escalation, then its approval.
    engine = _engine({"c": lambda entry, value: value}, calls)
    engine.submit(OpDefinition("gated", (OpStep("s", "c", "run", 1, gate=Gate(escalate="always")),)))
    escalation = engine.open_escalation("gated")["escalation"]
    engine.decide("gated", escalation=escalation, actor="alice", outcome="approve")
    # A worker that answers ok: false, and one that raises.
    _engine({"c": lambda entry, value: ResultEnvelope.failure("model-call-failed", "no")}, calls).submit(
        OpDefinition("refused", (OpStep("s", "c", "run"),)))

    def broken(entry, value):
        raise RuntimeError("down")

    _engine({"c": broken}, calls).submit(OpDefinition("broken", (OpStep("s", "c", "run"),)))
    # A budget stop.
    spender = {"c": lambda entry, value: ResultEnvelope.success(value, usage={"tokens": 10})}
    _engine(spender, calls, budget=RunBudget(max_tokens=5)).submit(
        OpDefinition("spent", (OpStep("a", "c", "run"), OpStep("b", "c", "run"))))
    _engine({"c": lambda entry, value: value}, calls, budget=RunBudget(max_duration=timedelta(0))).submit(
        OpDefinition("late", (OpStep("s", "c", "run"),)))
    assert set(calls) == set(STEP_FUNCTIONS)


def test_no_step_function_assigns_a_state_itself():
    # States move only through the machines' transitions, whose records the runner writes.
    tree = ast.parse((SOURCE / "runner.py").read_text())
    assigned = [
        ast.unparse(target) for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute) and target.attr == "state"
    ]
    assert assigned == []


def test_every_write_while_a_run_advances_reaches_the_pass_history():
    # Worker records from teardown, and a budget stop, included: a history that silently
    # lacks events would be a trap for anything that reads it later in the pass.
    class FailingTeardown(InMemoryCogExecutor):
        def teardown(self, worker):
            raise ConnectionError("delete failed")

    bypassed: list[str] = []

    def watched(runner: LifecycleRunner) -> LifecycleRunner:
        real = runner._append

        def append(run_id, event_type, payload, into=None):
            if into is None:
                bypassed.append(event_type)
            return real(run_id, event_type, payload, into)

        runner._append = append
        return runner

    spender = {"c": lambda entry, value: ResultEnvelope.success(value, usage={"tokens": 10})}
    watched(LifecycleRunner(executor=InMemoryCogExecutor(spender), track=InMemoryTrackStore(),
                                  budget=RunBudget(max_tokens=5))).submit(
        OpDefinition("spent", (OpStep("a", "c", "run"), OpStep("b", "c", "run"))))
    watched(LifecycleRunner(executor=FailingTeardown({"c": lambda entry, value: value}),
                                  track=InMemoryTrackStore())).submit(OpDefinition("leak", (OpStep("s", "c", "run"),)))
    gated = watched(LifecycleRunner(executor=InMemoryCogExecutor({"c": lambda entry, value: value}),
                                          track=InMemoryTrackStore()))
    gated.submit(OpDefinition("gated", (OpStep("s", "c", "run", gate=Gate(escalate="always")),)))
    gated.decide("gated", escalation=gated.open_escalation("gated")["escalation"], actor="alice", outcome="approve")
    assert bypassed == []


def test_fail_refuses_an_attempt_that_produced_a_result():
    # fail() is a step function a backend can call; its precondition is stated, not an AttributeError.
    from collab_hub_execution.runner import Attempt, _Pass

    runner = LifecycleRunner(executor=InMemoryCogExecutor({}), track=InMemoryTrackStore())
    attempt = Attempt(step=OpStep("s", "c", "run"), number=0, instance="s:0", key="r:s:0",
                      outcome=("ok", ResultEnvelope.success({"answer": 1})))
    with pytest.raises(ValueError, match="fail\\(\\) is for an attempt without a result"):
        runner.fail(_Pass(runner, "r", ()), None, attempt)
