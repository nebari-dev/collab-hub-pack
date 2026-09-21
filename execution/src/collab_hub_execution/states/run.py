"""The run: where a run is, from submission to its end.

A run's status is this machine folded over its Track (:meth:`Run.replay`), so
the API, the controller and a local run host derive it the same way.

The records keep the Track's current event names — ``paused`` for an
escalation, ``signal_received`` for a decision, ``timed_out`` for a duration
stop. Track event schema v1 (#5) renames them; the states do not change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ._machine import Context, InvalidTransition, Machine, Record, State, Transition, accepts, move

DIMENSIONS = frozenset({"duration", "tokens", "cost"})
DECISIONS = frozenset({"approve", "send_back", "reject"})


class StaleEscalation(InvalidTransition):
    """A decision that names an escalation other than the open one."""


class RunState(State):
    """The run machine's interface: one method per event, each refused by default."""

    def pickup(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("pickup")

    def escalate(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("escalate")

    def decide(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("decide")

    def complete(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("complete")

    def fail(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("fail")

    def exhaust_budget(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("exhaust_budget")

    def cancel(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("cancel")

    def host_stopped(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("host_stopped")

    def retry(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("retry")

    @property
    def ended(self) -> bool:
        """True when the run is not advancing: it has finished, or waits for a retry."""
        return not isinstance(self, (Submitted, Running, WaitingAtGate))



def _cancel(state: RunState, run: Run, actor: str | None) -> Transition[Run]:
    if not actor:
        state.refuse("cancel", "a cancellation names its actor")
    return move(run, RunState.CANCELLED, Record("cancelled", {"actor": actor}), open_step=None, open_escalation=None)


def _host_stopped(state: RunState, run: Run, backend: str) -> Transition[Run]:
    # Only `none` cannot resume a run; under `dbos` or `temporal` a host stop
    # leaves the run where it was, and the engine resumes it.
    if backend != "none":
        state.refuse("host_stopped", f"the {backend!r} backend resumes the run; only 'none' interrupts it")
    return move(run, RunState.INTERRUPTED, Record("interrupted", {"backend": backend}))


def _failed(step: str | None, error: str, reason: str | None, details: Mapping[str, Any] | None) -> Record:
    payload: dict[str, Any] = {} if step is None else {"step": step}
    payload["error"] = error
    if reason is not None:
        payload["reason"] = reason
    return Record("failed", {**payload, **(details or {})})


class Submitted(RunState):
    name = "SUBMITTED"

    @accepts("RUNNING")
    def pickup(self, run: Run, **_: Any) -> Transition[Run]:
        return move(run, RunState.RUNNING, Record("run_picked_up", {}))

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)


class Running(RunState):
    name = "RUNNING"

    @accepts("WAITING_AT_GATE", "FAILED")
    def escalate(
        self, run: Run, *, step: str, reason: str | None = None, escalation: str | None = None,
        revise_limit: int | None = None, **_: Any,
    ) -> Transition[Run]:
        if revise_limit is not None and run.escalations.get(step, 0) >= revise_limit:
            # The step has been revised `revise_limit` times and asks for another revision.
            # #35's signal cannot say whether it approves or sends back, so until a decision
            # carries its outcome (#99) this is where the limit is applied, as #35 applied it.
            stop = _failed(step, "revise_limit_exceeded", None, {"revise_limit": revise_limit})
            return move(run, RunState.FAILED, stop)
        payload: dict[str, Any] = {"step": step, "reason": reason}
        if escalation is not None:
            payload["escalation"] = escalation
        counts = dict(run.escalations)
        counts[step] = counts.get(step, 0) + 1
        return move(
            run, RunState.WAITING_AT_GATE, Record("paused", payload),
            open_step=step, open_escalation=escalation, escalations=counts,
        )

    @accepts("COMPLETED")
    def complete(self, run: Run, **_: Any) -> Transition[Run]:
        return move(run, RunState.COMPLETED, Record("completed", {}))

    @accepts("FAILED")
    def fail(
        self, run: Run, *, error: str, step: str | None = None, reason: str | None = None,
        details: Mapping[str, Any] | None = None, **_: Any,
    ) -> Transition[Run]:
        return move(run, RunState.FAILED, _failed(step, error, reason, details))

    @accepts("BUDGET_EXCEEDED")
    def exhaust_budget(
        self, run: Run, *, dimension: str, step: str | None = None, reason: str | None = None, **_: Any
    ) -> Transition[Run]:
        if dimension not in DIMENSIONS:
            self.refuse("exhaust_budget", f"unknown budget dimension {dimension!r}")
        payload: dict[str, Any] = {} if step is None else {"step": step}
        payload.update(reason=reason, dimension=dimension)
        event_type = "timed_out" if dimension == "duration" else "budget_exceeded"
        return move(run, RunState.BUDGET_EXCEEDED, Record(event_type, payload))

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)

    @accepts("INTERRUPTED")
    def host_stopped(self, run: Run, *, backend: str = "none", **_: Any) -> Transition[Run]:
        return _host_stopped(self, run, backend)


class WaitingAtGate(RunState):
    name = "WAITING_AT_GATE"

    @accepts("RUNNING", "REJECTED", "FAILED")
    def decide(
        self, run: Run, *, outcome: str, escalation: str | None = None, findings: Any = None,
        revise_limit: int | None = None, **_: Any,
    ) -> Transition[Run]:
        if outcome not in DECISIONS:
            self.refuse("decide", f"unknown outcome {outcome!r}")
        if escalation != run.open_escalation:
            raise StaleEscalation(
                self, "decide", f"it names escalation {escalation!r}, and the open one is {run.open_escalation!r}"
            )
        step = run.open_step
        closed = {"open_step": None, "open_escalation": None}
        # Every decision record names the escalation it answered, so replay answers the same one.
        answered = {} if escalation is None else {"escalation": escalation}
        if outcome == "reject":
            record = Record("rejected", {"step": step, "value": findings, **answered})
            return move(run, RunState.REJECTED, record, **closed)
        if outcome == "send_back" and revise_limit is not None and run.escalations.get(step, 0) > revise_limit:
            # The step has escalated `escalations` times, so a send back now would produce
            # revision number `escalations`: past the limit, the run fails instead.
            details = {"revise_limit": revise_limit, "value": findings, **answered}
            return move(run, RunState.FAILED, _failed(step, "revise_limit_exceeded", None, details), **closed)
        record = Record("signal_received", {"step": step, "outcome": outcome, "value": findings, **answered})
        return move(run, RunState.RUNNING, record, **closed)

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)

    @accepts("INTERRUPTED")
    def host_stopped(self, run: Run, *, backend: str = "none", **_: Any) -> Transition[Run]:
        return _host_stopped(self, run, backend)


class Completed(RunState):
    name = "COMPLETED"


class Failed(RunState):
    name = "FAILED"

    @accepts("RUNNING")
    def retry(self, run: Run, **_: Any) -> Transition[Run]:
        # A recorded failure runs again as a new attempt, under a new key.
        return move(run, RunState.RUNNING, Record("retry_requested", {"from_status": self.value, "attempt": "new"}))


class Rejected(RunState):
    name = "REJECTED"


class Cancelled(RunState):
    name = "CANCELLED"


class BudgetExceeded(RunState):
    name = "BUDGET_EXCEEDED"

    @accepts("RUNNING")
    def retry(self, run: Run, **_: Any) -> Transition[Run]:
        # The stop fell between steps, so no attempt was in flight; the budget starts again.
        payload = {"from_status": self.value, "attempt": "same", "budget_epoch": "new"}
        return move(run, RunState.RUNNING, Record("retry_requested", payload))


class Interrupted(RunState):
    name = "INTERRUPTED"

    @accepts("RUNNING")
    def retry(self, run: Run, **_: Any) -> Transition[Run]:
        # The attempt in flight continues under its key, so a committed claim answers.
        return move(run, RunState.RUNNING, Record("retry_requested", {"from_status": self.value, "attempt": "same"}))


RUN = Machine(
    "run",
    RunState,
    (Submitted, Running, WaitingAtGate, Completed, Failed, Rejected, Cancelled, BudgetExceeded, Interrupted),
    initial="SUBMITTED",
)


class TrackFact(Protocol):
    """What replay reads from a Track event."""

    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Run(Context):
    """A run: its state, the Gate it waits at, and how often each step escalated."""

    run_id: str
    state: RunState = field(default_factory=lambda: RUN.initial)  # type: ignore[assignment]
    open_step: str | None = None
    open_escalation: str | None = None
    escalations: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def submit(cls, run_id: str, op: Mapping[str, Any]) -> Transition[Run]:
        """A new run, and the record of its submission."""
        return Transition(cls(run_id=run_id), (Record("op_submitted", {"op": dict(op)}),))

    def pickup(self) -> Transition[Run]:
        return self.dispatch("pickup")

    def escalate(
        self, *, step: str, reason: str | None = None, escalation: str | None = None, revise_limit: int | None = None
    ) -> Transition[Run]:
        return self.dispatch("escalate", step=step, reason=reason, escalation=escalation, revise_limit=revise_limit)

    def decide(
        self, *, outcome: str, escalation: str | None = None, findings: Any = None, revise_limit: int | None = None
    ) -> Transition[Run]:
        return self.dispatch(
            "decide", outcome=outcome, escalation=escalation, findings=findings, revise_limit=revise_limit
        )

    def complete(self) -> Transition[Run]:
        return self.dispatch("complete")

    def fail(
        self, *, error: str, step: str | None = None, reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Transition[Run]:
        return self.dispatch("fail", error=error, step=step, reason=reason, details=details)

    def exhaust_budget(self, *, dimension: str, step: str | None = None, reason: str | None = None) -> Transition[Run]:
        return self.dispatch("exhaust_budget", dimension=dimension, step=step, reason=reason)

    def cancel(self, *, actor: str) -> Transition[Run]:
        return self.dispatch("cancel", actor=actor)

    def host_stopped(self, *, backend: str) -> Transition[Run]:
        return self.dispatch("host_stopped", backend=backend)

    def retry(self) -> Transition[Run]:
        return self.dispatch("retry")

    def apply(self, fact: TrackFact) -> Run:
        """The run after one recorded Track event.

        A fact about a step or a worker leaves the run where it is. A run event
        goes through the same handler as it did live, with the arguments the Track
        recorded, and must record what was recorded: the same event, with the same
        outcome, attempt, error and dimension. Anything else is refused.
        """
        if fact.event_type in _FACTS:
            return self
        replay = _REPLAY.get(fact.event_type)
        if replay is None:
            raise InvalidTransition(self.state, fact.event_type, "it is not an event of a run's Track")
        payload = fact.payload or {}
        transition = replay(self, payload)
        produced = transition.records[0] if transition.records else None
        if produced is None or produced.event_type != fact.event_type:
            written = produced.event_type if produced else "nothing"
            raise InvalidTransition(self.state, fact.event_type, f"replayed here, the machine records {written!r}")
        for key in _DECIDING:
            if key in payload and key in produced.payload and payload[key] != produced.payload[key]:
                raise InvalidTransition(
                    self.state, fact.event_type,
                    f"it records {key}={payload[key]!r}, and the machine records {produced.payload[key]!r}",
                )
        return transition.after

    @classmethod
    def replay(cls, facts: Iterable[TrackFact]) -> Run | None:
        """Fold a run's Track through the machine; ``None`` for a run never submitted.

        A Track the machine could not have written fails with
        :class:`InvalidTransition` instead of becoming a status. A Track with no
        ``run_picked_up`` at all was written before pickups were recorded: its first
        step start, or its first run event, is read as the pickup.
        """
        facts = tuple(facts)
        before_pickups = not any(fact.event_type == "run_picked_up" for fact in facts)
        run: Run | None = None
        for fact in facts:
            if fact.event_type in _SUBMISSIONS:
                if run is not None:
                    raise InvalidTransition(run.state, fact.event_type, "the run was already submitted")
                run = cls(run_id=getattr(fact, "run_id", ""))
            elif run is None:
                if fact.event_type in _REPLAY:
                    raise InvalidTransition(RUN.initial, fact.event_type, "the Track has no submission before it")
            else:
                if before_pickups and run.state is RunState.SUBMITTED and fact.event_type in _PICKED_UP_BY:
                    run = run.pickup().after
                run = run.apply(fact)
        return run


def _replay_failed(run: Run, payload: Mapping[str, Any]) -> Transition[Run]:
    if run.state is RunState.WAITING_AT_GATE and payload.get("error") == "revise_limit_exceeded":
        return run.decide(
            outcome="send_back", escalation=payload.get("escalation"), findings=payload.get("value"),
            revise_limit=payload.get("revise_limit"),
        )
    return run.fail(error=payload.get("error", ""), step=payload.get("step"), reason=payload.get("reason"))


def _replay_decision(outcome: str | None) -> Callable[[Run, Mapping[str, Any]], Transition[Run]]:
    def replay(run: Run, payload: Mapping[str, Any]) -> Transition[Run]:
        # #35's signal re-ran the paused step with its value: a send back.
        return run.decide(
            outcome=outcome or payload.get("outcome", "send_back"),
            escalation=payload.get("escalation"),
            findings=payload.get("value"),
        )

    return replay


# "submitted" is what older Tracks called the submission.
_SUBMISSIONS = frozenset({"op_submitted", "submitted"})

# Track events about a step or its worker: facts beside the run, not moves of it.
_FACTS = frozenset({
    "step_started", "materialized", "ready", "interaction_started", "interaction_usage",
    "idle", "teardown_started", "teardown_failed", "step_completed",
})

# The payload fields that decide where a replayed event leads; a record and its replay must agree on them.
_DECIDING = ("outcome", "attempt", "from_status", "error", "dimension")

# In a Track written before pickups were recorded, what showed the run had been picked up.
_PICKED_UP_BY = frozenset({
    "step_started", "paused", "signal_received", "rejected", "completed", "failed",
    "timed_out", "budget_exceeded", "interrupted", "retry_requested",
})

_REPLAY: dict[str, Callable[[Run, Mapping[str, Any]], Transition[Run]]] = {
    "run_picked_up": lambda run, payload: run.pickup(),
    "paused": lambda run, payload: run.escalate(
        step=payload.get("step", ""), reason=payload.get("reason"), escalation=payload.get("escalation")
    ),
    "signal_received": _replay_decision(None),
    "rejected": _replay_decision("reject"),
    "completed": lambda run, payload: run.complete(),
    "failed": _replay_failed,
    "timed_out": lambda run, payload: run.exhaust_budget(
        dimension="duration", step=payload.get("step"), reason=payload.get("reason")
    ),
    "budget_exceeded": lambda run, payload: run.exhaust_budget(
        # Older records did not say which spending limit stopped the run; the state is the same.
        dimension=payload.get("dimension", "tokens"), step=payload.get("step"), reason=payload.get("reason")
    ),
    "cancelled": lambda run, payload: run.cancel(actor=payload.get("actor", "")),
    "interrupted": lambda run, payload: run.host_stopped(backend=payload.get("backend", "none")),
    "retry_requested": lambda run, payload: run.retry(),
}
