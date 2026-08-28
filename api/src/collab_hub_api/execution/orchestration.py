"""Durable multi-step Op orchestration behind a replaceable engine contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .lifecycle import CogLifecycle, LifecycleState
from .track import RunStatus, TrackEvent, TrackStore, derive_run_status


@dataclass(frozen=True, slots=True)
class OpStep:
    """One interaction with a Cog entry point."""

    name: str
    cog: str
    entry_point: str
    input: Any = None


@dataclass(frozen=True, slots=True)
class OpDefinition:
    """A serializable, multi-step Op definition."""

    run_id: str
    steps: tuple[OpStep, ...]


class PauseRequest(Exception):
    """A Cog's request for an external signal before continuing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CogWorker(Protocol):
    def interact(self, entry_point: str, input: Any = None) -> Any:
        """Interact through a declared entry point."""


class CogExecutor(Protocol):
    def materialize(self, cog: str, run_id: str) -> CogWorker:
        """Materialize one Cog worker for a run."""

    def teardown(self, worker: CogWorker) -> None:
        """Release a materialized worker."""


class WorkflowEngine(Protocol):
    """The stable boundary used by callers, independent of engine choice."""

    def submit(self, op: OpDefinition) -> RunStatus:
        """Start or recover an Op."""

    def signal(self, run_id: str, value: Any = None) -> RunStatus:
        """Resume a paused Op with an external value."""

    def observe(self, run_id: str) -> RunStatus:
        """Return status reconstructed from the Track."""


class InMemoryCogExecutor(CogExecutor):
    """A fake executor for exercising orchestration without infrastructure."""

    def __init__(self, handlers: dict[str, Callable[[str, Any], Any]]) -> None:
        self.handlers = handlers
        self.materialized: list[tuple[str, str]] = []
        self.torn_down: list[str] = []

    def materialize(self, cog: str, run_id: str) -> CogWorker:
        self.materialized.append((run_id, cog))
        return _Worker(cog, self.handlers[cog])

    def teardown(self, worker: CogWorker) -> None:
        self.torn_down.append(worker.cog)  # type: ignore[attr-defined]


class _Worker:
    def __init__(self, cog: str, handler: Callable[[str, Any], Any]) -> None:
        self.cog = cog
        self.handler = handler

    def interact(self, entry_point: str, input: Any = None) -> Any:
        return self.handler(entry_point, input)


class DurableWorkflowEngine(WorkflowEngine):
    """An engine whose recovery source is exclusively the Track."""

    def __init__(self, *, executor: CogExecutor, track: TrackStore) -> None:
        self.executor = executor
        self.track = track

    def _append(self, run_id: str, event_type: str, **payload: Any) -> None:
        self.track.append(TrackEvent(run_id=run_id, event_type=event_type, payload=payload))

    def observe(self, run_id: str) -> RunStatus:
        return derive_run_status(self.track.replay(run_id))

    def _submitted_definition(self, run_id: str) -> OpDefinition:
        for event in self.track.replay(run_id):
            if event.event_type == "op_submitted":
                return _deserialize_op(event.payload["op"])
        raise LookupError(f"no submitted Op for run {run_id!r}")

    def _completed_steps(self, run_id: str) -> set[str]:
        return {event.payload["step"] for event in self.track.replay(run_id) if event.event_type == "step_completed"}

    def submit(self, op: OpDefinition) -> RunStatus:
        existing = self.track.replay(op.run_id)
        if not existing:
            self._append(op.run_id, "op_submitted", op=_serialize_op(op))
            self._append(op.run_id, "submitted")
        elif self._submitted_definition(op.run_id) != op:
            raise ValueError(f"run {op.run_id!r} was submitted with a different Op")
        return self._advance(op)

    def _advance(self, op: OpDefinition, signal: Any = None) -> RunStatus:
        completed = self._completed_steps(op.run_id)
        for step in op.steps:
            if step.name in completed:
                continue
            self._append(op.run_id, "step_started", step=step.name, cog=step.cog)
            lifecycle = CogLifecycle()
            worker = self.executor.materialize(step.cog, op.run_id)
            self._append(op.run_id, "materialized", cog=step.cog)
            lifecycle.transition(LifecycleState.READY)
            self._append(op.run_id, "ready", cog=step.cog)
            lifecycle.transition(LifecycleState.INTERACTING)
            self._append(op.run_id, "interaction_started", step=step.name, entry_point=step.entry_point)
            try:
                value = worker.interact(step.entry_point, step.input if signal is None else signal)
            except PauseRequest as pause:
                self._append(op.run_id, "paused", step=step.name, reason=pause.reason)
                self.executor.teardown(worker)
                return RunStatus.PAUSED
            except Exception as exc:
                self._append(op.run_id, "failed", step=step.name, error=type(exc).__name__)
                self.executor.teardown(worker)
                return RunStatus.FAILED
            lifecycle.transition(LifecycleState.IDLE)
            self._append(op.run_id, "idle", step=step.name)
            lifecycle.transition(LifecycleState.TEARING_DOWN)
            self._append(op.run_id, "teardown_started", step=step.name)
            self.executor.teardown(worker)
            lifecycle.transition(LifecycleState.TORN_DOWN)
            self._append(op.run_id, "step_completed", step=step.name, output=value)
            signal = None
        self._append(op.run_id, "completed")
        return RunStatus.COMPLETED

    def signal(self, run_id: str, value: Any = None) -> RunStatus:
        op = self._submitted_definition(run_id)
        if self.observe(run_id) is not RunStatus.PAUSED:
            raise ValueError(f"run {run_id!r} is not paused")
        return self._advance(op, signal=value)


def _serialize_op(op: OpDefinition) -> dict[str, Any]:
    return {
        "run_id": op.run_id,
        "steps": [
            {"name": step.name, "cog": step.cog, "entry_point": step.entry_point, "input": step.input}
            for step in op.steps
        ],
    }


def _deserialize_op(value: dict[str, Any]) -> OpDefinition:
    return OpDefinition(
        run_id=value["run_id"],
        steps=tuple(
            OpStep(
                name=step["name"],
                cog=step["cog"],
                entry_point=step["entry_point"],
                input=step.get("input"),
            )
            for step in value["steps"]
        ),
    )
