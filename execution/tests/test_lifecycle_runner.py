"""The lifecycle runner: the lifecycle lives in its step functions, and the engine only delegates to it."""

from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from collab_hub_execution import (
    DurableWorkflowEngine,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
    RunState,
)
from collab_hub_execution.orchestration import STEP_FUNCTIONS, LifecycleRunner

SOURCE = Path(__file__).resolve().parents[1] / "src" / "collab_hub_execution"


def test_the_step_functions_are_registered():
    assert list(STEP_FUNCTIONS) == [
        "resolve", "materialize", "interact", "read_envelope", "evaluate_gate",
        "complete", "complete_approved", "escalate", "fail", "teardown", "stop_for_budget",
    ]
    assert all(getattr(LifecycleRunner, name) is function for name, function in STEP_FUNCTIONS.items())


def _spied(engine: DurableWorkflowEngine, calls: list[str]) -> DurableWorkflowEngine:
    runner = engine.runner
    for name in STEP_FUNCTIONS:
        bound = getattr(runner, name)

        def spy(*args, _bound=bound, _name=name, **kwargs):
            calls.append(_name)
            return _bound(*args, **kwargs)

        setattr(runner, name, spy)
    return engine


def _engine(handlers, calls, **kwargs) -> DurableWorkflowEngine:
    return _spied(DurableWorkflowEngine(executor=InMemoryCogExecutor(handlers), track=InMemoryTrackStore(),
                                        **kwargs), calls)


def test_a_completed_step_goes_through_the_runner_s_step_functions():
    calls: list[str] = []
    engine = _engine({"c": lambda entry, value: {"ok": value}}, calls)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", 1),))) is RunState.COMPLETED
    assert calls == ["resolve", "materialize", "interact", "read_envelope", "teardown", "evaluate_gate", "complete"]


def test_every_step_function_is_reached_through_the_engine():
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


@pytest.mark.parametrize("method", ["submit", "retry", "decide", "observe", "open_escalation", "_budget_tracker"])
def test_the_engine_makes_no_lifecycle_decision_it_only_delegates(method):
    # Each engine method is one return of the runner's method of the same name.
    tree = ast.parse(inspect.getsource(getattr(DurableWorkflowEngine, method)).strip())
    [function] = tree.body
    body = [node for node in function.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Return), ast.dump(function)
    call = body[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert ast.unparse(call.func.value) == "self.runner"
    assert call.func.attr == method.lstrip("_")


def test_no_step_function_assigns_a_state_itself():
    # States move only through the machines' transitions, whose records the runner writes.
    tree = ast.parse((SOURCE / "runner.py").read_text())
    assigned = [
        ast.unparse(target) for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute) and target.attr == "state"
    ]
    assert assigned == []
