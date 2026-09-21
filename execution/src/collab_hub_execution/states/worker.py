"""A worker: one materialization of a Cog, from its first answer to its teardown.

It replaces #35's ``CogLifecycle`` transition table. Its records are the
Track events the engine already writes for a step's worker; ``TORN_DOWN`` is
never recorded — the Track records a teardown that failed, not one that worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._machine import Context, Machine, Record, State, Transition, accepts, move

if TYPE_CHECKING:
    from .install import CogInstall

# Why a worker is torn down, by the state it is torn down from (docs/cog-execution/states.md).
TEARDOWN_REASONS = {
    "READY": frozenset({"cancel", "orphan_reaped"}),
    "INTERACTING": frozenset({"cancel", "deadline", "orphan_reaped"}),
    "IDLE": frozenset({"one_shot", "idle_timeout", "orphan_reaped"}),
}


class WorkerState(State):
    """The worker machine's interface: one method per event, each refused by default."""

    def ready(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("ready")

    def invoke(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("invoke")

    def envelope_returned(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("envelope_returned")

    def tear_down(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("tear_down")

    def torn_down(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("torn_down")

    def fail(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return self.refuse("fail")



def _invoke(worker: Worker, step: str | None, entry_point: str) -> Transition[Worker]:
    step = worker.step if step is None else step
    record = Record("interaction_started", {"step": step, "entry_point": entry_point})
    return move(worker, WorkerState.INTERACTING, record, step=step)


def _tear_down(state: WorkerState, worker: Worker, reason: str) -> Transition[Worker]:
    allowed = TEARDOWN_REASONS[state.name]
    if reason not in allowed:
        state.refuse("tear_down", f"{reason!r} is not a reason to tear down from {state.name}: {sorted(allowed)}")
    return move(worker, WorkerState.TEARING_DOWN, Record("teardown_started", {"step": worker.step, "reason": reason}))


def _fail(worker: Worker, error: str) -> Transition[Worker]:
    return move(worker, WorkerState.WORKER_FAILED, error=error)


class Materialized(WorkerState):
    name = "MATERIALIZED"

    @accepts("READY")
    def ready(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return move(worker, WorkerState.READY, Record("ready", {"cog": worker.cog}))

    @accepts("WORKER_FAILED")
    def fail(self, worker: Worker, *, error: str, **_: Any) -> Transition[Worker]:
        return _fail(worker, error)


class Ready(WorkerState):
    name = "READY"

    @accepts("INTERACTING")
    def invoke(self, worker: Worker, *, entry_point: str, step: str | None = None, **_: Any) -> Transition[Worker]:
        return _invoke(worker, step, entry_point)

    @accepts("TEARING_DOWN")
    def tear_down(self, worker: Worker, *, reason: str, **_: Any) -> Transition[Worker]:
        return _tear_down(self, worker, reason)

    @accepts("WORKER_FAILED")
    def fail(self, worker: Worker, *, error: str, **_: Any) -> Transition[Worker]:
        return _fail(worker, error)


class Interacting(WorkerState):
    name = "INTERACTING"

    @accepts("IDLE")
    def envelope_returned(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return move(worker, WorkerState.IDLE, Record("idle", {"step": worker.step}))

    @accepts("TEARING_DOWN")
    def tear_down(self, worker: Worker, *, reason: str, **_: Any) -> Transition[Worker]:
        return _tear_down(self, worker, reason)

    @accepts("WORKER_FAILED")
    def fail(self, worker: Worker, *, error: str, **_: Any) -> Transition[Worker]:
        return _fail(worker, error)


class Idle(WorkerState):
    name = "IDLE"

    @accepts("INTERACTING")
    def invoke(self, worker: Worker, *, entry_point: str, step: str | None = None, **_: Any) -> Transition[Worker]:
        # A warm worker takes the next step.
        return _invoke(worker, step, entry_point)

    @accepts("TEARING_DOWN")
    def tear_down(self, worker: Worker, *, reason: str, **_: Any) -> Transition[Worker]:
        return _tear_down(self, worker, reason)

    @accepts("WORKER_FAILED")
    def fail(self, worker: Worker, *, error: str, **_: Any) -> Transition[Worker]:
        return _fail(worker, error)


class TearingDown(WorkerState):
    name = "TEARING_DOWN"

    @accepts("TORN_DOWN")
    def torn_down(self, worker: Worker, **_: Any) -> Transition[Worker]:
        return move(worker, WorkerState.TORN_DOWN)

    @accepts("WORKER_FAILED")
    def fail(self, worker: Worker, *, error: str, **_: Any) -> Transition[Worker]:
        # A worker that could not be torn down may still be running: that is recorded.
        record = Record("teardown_failed", {"step": worker.step, "error": error})
        return move(worker, WorkerState.WORKER_FAILED, record, error=error)


class TornDown(WorkerState):
    name = "TORN_DOWN"


class WorkerFailed(WorkerState):
    name = "WORKER_FAILED"


WORKER = Machine(
    "worker",
    WorkerState,
    (Materialized, Ready, Interacting, Idle, TearingDown, TornDown, WorkerFailed),
    initial="MATERIALIZED",
)


@dataclass(frozen=True, slots=True)
class Worker(Context):
    """One materialized worker of a Cog, and the step it is serving."""

    cog: str
    step: str | None = None
    digest: str | None = None
    state: WorkerState = field(default_factory=lambda: WORKER.initial)  # type: ignore[assignment]
    error: str | None = None

    @classmethod
    def materialize(
        cls, cog: str, *, step: str | None = None, digest: str | None = None, install: CogInstall | None = None
    ) -> Transition[Worker]:
        """A worker the executor has just brought up, and the record of it.

        Only an ``INVOKABLE`` install is materialized. A development package
        read from a directory has no install, and passes none.
        """
        if install is not None:
            install.require_invokable()
        record = Record("materialized", {"cog": cog, "digest": digest})
        return Transition(cls(cog=cog, step=step, digest=digest), (record,))

    def ready(self) -> Transition[Worker]:
        return self.dispatch("ready")

    def invoke(self, *, entry_point: str, step: str | None = None) -> Transition[Worker]:
        return self.dispatch("invoke", entry_point=entry_point, step=step)

    def envelope_returned(self) -> Transition[Worker]:
        return self.dispatch("envelope_returned")

    def tear_down(self, *, reason: str) -> Transition[Worker]:
        return self.dispatch("tear_down", reason=reason)

    def torn_down(self) -> Transition[Worker]:
        return self.dispatch("torn_down")

    def fail(self, *, error: str) -> Transition[Worker]:
        return self.dispatch("fail", error=error)
