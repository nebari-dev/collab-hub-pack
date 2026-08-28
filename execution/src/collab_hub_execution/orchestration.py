"""Durable multi-step Op orchestration behind a replaceable engine contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .lifecycle import (
    BudgetExceeded,
    BudgetTracker,
    CogLifecycle,
    LifecycleState,
    RunBudget,
)
from .track import RunStatus, TrackEvent, TrackStore, derive_run_status


@dataclass(frozen=True, slots=True)
class OpStep:
    """One interaction with a Cog entry point."""

    name: str
    cog: str
    entry_point: str
    input: Any = None
    digest: str | None = None


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
    def interact(self, entry_point: str, input: Any = None, idempotency_key: str | None = None) -> Any:
        """Interact through a declared entry point.

        ``idempotency_key`` is stable per (run, step, attempt) so a Cog can dedupe
        a retried interaction after a crash-recovery. (A durable claim that makes
        the crash window fully safe lands with the DBOS engine backing — #1.)
        """


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

    def interact(self, entry_point: str, input: Any = None, idempotency_key: str | None = None) -> Any:
        return self.handler(entry_point, input)


class DurableWorkflowEngine(WorkflowEngine):
    """An engine whose recovery source is exclusively the Track.

    Single-owner by assumption: it holds no cross-replica lease, so the same run
    must not be advanced from two API replicas concurrently. Multi-replica
    single-owner execution (an advancement lease) is provided by the DBOS engine
    backing (#1); the Postgres Track's one-submission-per-run index guards only a
    duplicated *submission*, not concurrent *advancement*.
    """

    def __init__(
        self,
        *,
        executor: CogExecutor,
        track: TrackStore,
        budget: RunBudget | None = None,
        max_revisions: int | None = None,
    ) -> None:
        self.executor = executor
        self.track = track
        self.budget = budget
        self.max_revisions = max_revisions

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

    def _budget_tracker(self, run_id: str) -> BudgetTracker | None:
        """Reconstruct the run's budget from the Track so it survives restarts."""
        if self.budget is None:
            return None
        events = self.track.replay(run_id)
        started_at = next((e.occurred_at for e in events if e.event_type == "submitted"), None)
        tracker = BudgetTracker(self.budget, started_at=started_at)
        for event in events:
            if event.event_type == "step_completed":
                usage = event.payload.get("usage") or {}
                tracker.tokens += int(usage.get("tokens", 0))
                tracker.cost += float(usage.get("cost", 0.0))
        return tracker

    def _pause_count(self, run_id: str, step: str) -> int:
        return sum(
            1
            for e in self.track.replay(run_id)
            if e.event_type == "paused" and e.payload.get("step") == step
        )

    def submit(self, op: OpDefinition) -> RunStatus:
        names = [step.name for step in op.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"Op {op.run_id!r} has duplicate step names: {names}")
        existing = self.track.replay(op.run_id)
        if not existing:
            self._append(op.run_id, "op_submitted", op=_serialize_op(op))
            self._append(op.run_id, "submitted")
        elif self._submitted_definition(op.run_id) != op:
            raise ValueError(f"run {op.run_id!r} was submitted with a different Op")
        return self._advance(op)

    def _advance(self, op: OpDefinition, signal: Any = None) -> RunStatus:
        completed = self._completed_steps(op.run_id)
        tracker = self._budget_tracker(op.run_id)
        for step in op.steps:
            if step.name in completed:
                continue
            if tracker is not None:
                try:
                    tracker.check()
                except BudgetExceeded as exc:
                    self._append(op.run_id, "budget_exceeded", step=step.name, reason=str(exc))
                    return RunStatus.BUDGET_EXCEEDED
            # attempt = prior pauses (real revisions), NOT the step_started count:
            # a crash before the outcome is recorded re-runs with the SAME key, so
            # a worker can deduplicate the replay.
            attempt = self._pause_count(op.run_id, step.name)
            self._append(op.run_id, "step_started", step=step.name, cog=step.cog, digest=step.digest, attempt=attempt)
            lifecycle = CogLifecycle()
            worker = None
            outcome: tuple[str, Any] = ("failed", "Unknown")
            teardown_error: str | None = None
            # Materialize, interact, and teardown are all inside failure handling
            # so any infra error becomes a durable `failed` event (never a
            # non-terminal run); teardown is best-effort in `finally`.
            try:
                worker = self.executor.materialize(step.cog, op.run_id)
                self._append(op.run_id, "materialized", cog=step.cog, digest=step.digest)
                lifecycle.transition(LifecycleState.READY)
                self._append(op.run_id, "ready", cog=step.cog)
                lifecycle.transition(LifecycleState.INTERACTING)
                self._append(op.run_id, "interaction_started", step=step.name, entry_point=step.entry_point)
                key = f"{op.run_id}:{step.name}:{attempt}"
                arg = step.input if signal is None else signal
                try:
                    value = worker.interact(step.entry_point, arg, idempotency_key=key)
                    outcome = ("ok", value)
                except PauseRequest as pause:
                    outcome = ("pause", pause.reason)
            except Exception as exc:  # noqa: BLE001 - any materialize/interact failure is durable-failed
                outcome = ("failed", type(exc).__name__)
            finally:
                if worker is not None:
                    try:
                        self.executor.teardown(worker)
                    except Exception as exc:  # noqa: BLE001 - never crash on cleanup
                        teardown_error = type(exc).__name__
                        self._append(op.run_id, "teardown_failed", step=step.name, error=teardown_error)

            if teardown_error is not None:
                # A worker we couldn't tear down may keep running/serving — that is
                # a leak, not success. Fail the run so it is visible; durable
                # cleanup-retry lands with the DBOS backing (#1).
                self._append(op.run_id, "failed", step=step.name, error="TeardownFailed")
                return RunStatus.FAILED

            kind, detail = outcome
            if kind == "failed":
                self._append(op.run_id, "failed", step=step.name, error=detail)
                return RunStatus.FAILED
            if kind == "pause":
                if self.max_revisions is not None and self._pause_count(op.run_id, step.name) >= self.max_revisions:
                    self._append(op.run_id, "failed", step=step.name, error="RevisionLimitExceeded")
                    return RunStatus.FAILED
                self._append(op.run_id, "paused", step=step.name, reason=detail)
                return RunStatus.PAUSED

            value = detail
            lifecycle.transition(LifecycleState.IDLE)
            self._append(op.run_id, "idle", step=step.name)
            lifecycle.transition(LifecycleState.TEARING_DOWN)
            self._append(op.run_id, "teardown_started", step=step.name)
            lifecycle.transition(LifecycleState.TORN_DOWN)
            usage = dict(value["usage"]) if isinstance(value, Mapping) and "usage" in value else None
            self._append(op.run_id, "step_completed", step=step.name, output=value, usage=usage)
            if tracker is not None and usage is not None:
                try:
                    tracker.consume(tokens=int(usage.get("tokens", 0)), cost=float(usage.get("cost", 0.0)))
                except BudgetExceeded as exc:
                    self._append(op.run_id, "budget_exceeded", step=step.name, reason=str(exc))
                    return RunStatus.BUDGET_EXCEEDED
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
            {
                "name": step.name,
                "cog": step.cog,
                "entry_point": step.entry_point,
                "input": step.input,
                "digest": step.digest,
            }
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
                digest=step.get("digest"),
            )
            for step in value["steps"]
        ),
    )
