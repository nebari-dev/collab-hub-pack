"""The lifecycle runner: a Cog's lifecycle, written once, as plain step functions.

ADR-0002 D1 puts the lifecycle in one component that every durability backend
schedules. This is that component. A run advances step by step through the
functions registered in ``STEP_FUNCTIONS``. An attempt resolves its identity,
materializes its worker, interacts and reads the envelope, and then tears the
worker down, whatever happened: the worker is one-shot. Only then does the step
end — failed if it produced no result, or through its Gate, which completes it
or escalates it. Budgets and Track recording surround them. Each function
moves the state machines of ``states`` by asking them for a transition and
writing the records it returns; none assigns a state itself.

A durability backend decides only how these functions are scheduled and whether
progress between them is checkpointed. Until one exists, ``DurableWorkflowEngine``
calls them in process and recovers from the Track, which Phase 8 of the plan
removes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .envelope import EnvelopeInvalid, ResultEnvelope
from .gates import DEFAULT_APPROVERS, Gate, GateOutcome, envelope_digest, escalation_id
from .lifecycle import BudgetExceeded, BudgetTracker, RunBudget
from .ops import (
    _NO_SIGNAL,
    CogExecutor,
    CogWorker,
    OpDefinition,
    OpStep,
    _canonical_op,
    _deserialize_op,
    _serialize_op,
)
from .states import RUN, Run, RunState, Transition, Worker
from .track import PAYLOAD_INLINE_MAX_BYTES, SCHEMA_VERSION, TrackEvent, TrackStore, upgrade

# A failure's message on the Track is bounded, so a stack trace or a model's
# answer cannot turn the accountability record into a log.
MESSAGE_MAX_CHARS = 1024

def _bound(text: Any) -> str | None:
    """A failure's text as the Track keeps it: at most ``MESSAGE_MAX_CHARS`` characters."""
    return None if text is None else str(text)[:MESSAGE_MAX_CHARS]


def _render(payload: Any) -> str:
    """A step's payload as JSON, once: what the size check measures and a store keeps.

    The envelope contract says a payload is JSON. One that is not fails the step
    as an invalid envelope, durably, instead of failing later in whichever store
    happens to serialize it.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise EnvelopeInvalid(f"the payload is not JSON: {exc}") from exc


def _payload_digest(payload: Any) -> str:
    """The sha256 of a payload's canonical JSON: sorted keys, compact separators.

    Canonical, not the bytes that were kept, so anyone can recompute it from the
    stored payload: Postgres keeps it as ``jsonb``, which does not keep key order.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _key_component(value: str) -> str:
    """Percent-escape the key delimiter so idempotency keys are injective.

    The key is ``{run_id}:{step}:{attempt}``; without escaping, ``run="a:b"
    step="c"`` and ``run="a" step="b:c"`` collide. Escaping ``:`` (and ``%``, so
    the escaping itself round-trips) keeps distinct (run, step) pairs distinct —
    the contract the exactly-once claim in #1 is built on — while leaving keys
    readable whenever the ids contain no ``:``.
    """
    return value.replace("%", "%25").replace(":", "%3A")


class UsageUnavailable(ValueError):
    """A configured budget cannot be accounted for, or usage is malformed."""


def _recorded(envelope: ResultEnvelope) -> dict[str, Any]:
    """The envelope fields the Track keeps beside a step's outcome, when present.

    ``problems`` are what a Gate decides on; ``binding`` is what makes the
    outcome auditable. Both are optional in the envelope, so the event only
    carries them when the worker reported them.
    """
    extra: dict[str, Any] = {}
    if envelope.problems:
        extra["problems"] = [{"check": p.check, "detail": p.detail, "severity": p.severity} for p in envelope.problems]
    if envelope.binding is not None:
        extra["binding"] = dict(envelope.binding)
    return extra


def _validate_usage(raw: Any, budget: RunBudget | None) -> dict[str, Any] | None:
    if raw is not None and not isinstance(raw, Mapping):
        raise UsageUnavailable("usage must be an object")
    usage = dict(raw) if raw is not None else {}
    for name in ("tokens", "cost"):
        if name not in usage:
            if budget is not None and getattr(budget, f"max_{name}") is not None:
                raise UsageUnavailable(f"missing {name} usage for configured budget")
            continue
        value = usage[name]
        valid = type(value) is int if name == "tokens" else type(value) in (int, float)
        if not valid or value < 0 or (type(value) is float and not math.isfinite(value)):
            raise UsageUnavailable(f"invalid {name} usage")
        if name == "cost":
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise UsageUnavailable("invalid cost usage")
    return {key: usage[key] for key in ("tokens", "cost") if key in usage} if raw is not None else None


def _escalation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One escalation as a caller reads it, filled in for a Track written before Gates.

    An escalation recorded before Gates carries only the step and the reason: the
    Cog asked for the pause, so there is no id, no envelope and no approvers. It
    still answers to a decision — naming its id, ``None`` — and an approval re-runs
    the step, since no envelope was recorded to complete it with.
    """
    return {
        "escalation": payload.get("escalation"),
        "step": payload.get("step"),
        "reason": payload.get("reason"),
        "attempt": payload.get("attempt"),
        "envelope": payload.get("envelope"),
        "payload_ref": payload.get("payload_ref"),
        "usage": payload.get("usage"),
        "approvers": payload.get("approvers", list(DEFAULT_APPROVERS)),
        "gate": payload.get("gate", Gate().escalate),
    }


STEP_FUNCTIONS: dict[str, Callable[..., Any]] = {}
"""The runner's step functions, by name, in the order an attempt reaches them.

``resolve``, ``materialize``, ``interact``, ``read_envelope``, then ``teardown``
of the one-shot worker whatever happened; then the step ends: ``evaluate_gate``
and ``complete`` or ``escalate`` when it produced a result, ``fail`` when it did
not. Outside an attempt, ``complete_approved`` completes a step from an approved
escalation, and ``stop_for_budget`` stops a run at a boundary.

The lifecycle is these and nothing else: a durability backend schedules them and
contains no lifecycle logic of its own, and a test holds every backend to calling
the same ones.
"""


def step_function(function: Callable[..., Any]) -> Callable[..., Any]:
    """Register one of the runner's step functions."""
    STEP_FUNCTIONS[function.__name__] = function
    return function


@dataclass
class Attempt:
    """One step attempt, as the step functions hand it on to each other."""

    step: OpStep
    number: int
    instance: str
    key: str
    worker: CogWorker | None = None
    cog_worker: Worker | None = None
    invoked: bool = False
    answered: bool = False
    accounted: bool = False
    result: Any = None
    rendered: str = ""
    usage: dict[str, Any] | None = None
    outcome: tuple[str, Any] = ("broken", "Unknown")
    failure_reason: str | None = None
    teardown_error: str | None = None


class _Pass:
    """One advance of a run: the Track as it was read, and everything written since.

    Every write goes through ``append`` or ``record`` into ``history``, so a step
    reads what the ones before it wrote without reading the Track again.
    """

    def __init__(self, runner: LifecycleRunner, run_id: str, events: Sequence[TrackEvent]) -> None:
        self.runner = runner
        self.run_id = run_id
        self.history = list(events)

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.runner._append(self.run_id, event_type, payload, self.history)

    def record(self, transition: Transition[Any]) -> Any:
        return self.runner._record(self.run_id, transition, self.history)


class LifecycleRunner:
    """Runs Ops step by step through the step functions, recording every move on the Track.

    Experimental: interfaces may change. ``submit()``, ``decide()`` and ``retry()``
    run synchronously until the run completes, fails, or waits at a Gate.

    Every change of a run's or a worker's state is a transition of its machine
    (``states/``): the runner asks, and writes the records the machine returns.
    """

    def __init__(
        self,
        *,
        executor: CogExecutor,
        track: TrackStore,
        budget: RunBudget | None = None,
        max_revisions: int | None = None,
        payload_inline_max_bytes: int = PAYLOAD_INLINE_MAX_BYTES,
    ) -> None:
        self.executor = executor
        self.track = track
        self.budget = budget
        self.max_revisions = max_revisions
        self.payload_inline_max_bytes = payload_inline_max_bytes

    def _append(self, run_id: str, event_type: str, payload: Mapping[str, Any],
                into: list[TrackEvent] | None = None) -> TrackEvent:
        """The one write path: store one event, and keep any snapshot it belongs to current."""
        event = self.track.append(TrackEvent(run_id=run_id, event_type=event_type, payload=dict(payload),
                                             schema=SCHEMA_VERSION))
        if into is not None:
            into.append(event)
        return event

    def _result(self, run_id: str, key: str, payload: Any, rendered: str) -> dict[str, Any]:
        """The result as an event carries it: ``payload`` inline, or ``payload_ref`` above the threshold.

        A large result is kept under the attempt's idempotency key, so recovering
        an attempt rewrites the same row instead of leaving another one behind, and
        an escalated result approved later is completed from the row already kept.
        """
        if len(rendered.encode()) > self.payload_inline_max_bytes:
            self.track.put_payload(run_id, key, rendered)
            return {"payload_ref": key}
        return {"payload": payload}

    def _step_completed(self, step: OpStep, attempt: int, envelope: ResultEnvelope,
                        usage: Mapping[str, Any] | None, result: Mapping[str, Any],
                        escalation: str | None = None) -> dict[str, Any]:
        """A ``step_completed`` payload: what produced the result, and the result inline or by reference."""
        record: dict[str, Any] = {
            "step": step.name, "attempt": attempt, "cog": step.cog, "digest": step.digest, "usage": usage,
            "frames": [], **_recorded(envelope), **result,
        }
        if escalation is not None:
            record["escalation"] = escalation
        return record

    def _step_failed(self, step: OpStep, attempt: int, key: str, error: str, message: str | None,
                     envelope: ResultEnvelope | None = None) -> dict[str, Any]:
        """A ``step_failed`` payload: the attempt, its key, the worker, the code and a bounded message."""
        record: dict[str, Any] = {
            "step": step.name, "attempt": attempt, "key": key, "cog": step.cog, "digest": step.digest,
            "error": error, "message": _bound(message) or "",
        }
        if envelope is not None:
            record.update(_recorded(envelope))
        return record

    def _record(self, run_id: str, transition: Transition[Any], into: list[TrackEvent] | None = None) -> Any:
        """Write what a transition reports, and return the context after it.

        ``into`` collects the events written, so a caller holding a snapshot of the
        Track advances on what it read *and* wrote, without reading it again.
        """
        for record in transition.records:
            self._append(run_id, record.event_type, record.payload, into)
        return transition.after

    def _read(self, run_id: str) -> tuple[TrackEvent, ...]:
        """The run's Track, read once and in schema v1 whatever version it was written in."""
        return tuple(upgrade(event) for event in self.track.replay(run_id))

    def observe(self, run_id: str) -> RunState | None:
        run = Run.replay(self._read(run_id))
        return None if run is None else run.state

    # The helpers below read one snapshot of the Track, taken once per call, so a
    # step costs no further reads of it.

    @staticmethod
    def _submitted_definition(run_id: str, events: tuple[TrackEvent, ...]) -> OpDefinition:
        for event in events:
            if event.event_type == "op_submitted":
                return _deserialize_op(event.payload["op"])
        raise LookupError(f"no submitted Op for run {run_id!r}")

    @staticmethod
    def _completed_steps(events: tuple[TrackEvent, ...]) -> set[str]:
        return {event.payload["step"] for event in events if event.event_type == "step_completed"}

    def budget_tracker(self, events: tuple[TrackEvent, ...]) -> BudgetTracker | None:
        """Reconstruct the run's budget from the Track so it survives restarts."""
        if self.budget is None:
            return None
        # New runs use op_submitted alone. Accept submitted for old Tracks.
        started_at = next(
            (e.occurred_at for e in events if e.event_type in ("submitted", "op_submitted")),
            None,
        )
        tracker = BudgetTracker(self.budget, started_at=started_at)
        accounted_steps = {e.payload.get("step") for e in events if e.event_type == "interaction_usage"}
        for event in events:
            if event.event_type == "interaction_usage" or (
                event.event_type == "step_completed" and event.payload.get("step") not in accounted_steps
            ):
                usage = _validate_usage(event.payload.get("usage"), self.budget) or {}
                tracker.tokens += int(usage.get("tokens", 0))
                tracker.cost += float(usage.get("cost", 0.0))
        return tracker

    @staticmethod
    def _retry_count(events: tuple[TrackEvent, ...]) -> int:
        # Only a retry that opens a new attempt moves the key on; one that
        # continues an interrupted attempt keeps it, so a committed claim answers.
        return sum(
            1 for e in events if e.event_type == "retry_requested" and e.payload.get("attempt", "new") == "new"
        )

    @staticmethod
    def _signal_for(events: tuple[TrackEvent, ...], step: str) -> Any:
        """The findings of the latest send back of a step, or ``_NO_SIGNAL``.

        Recovery re-reads the decision from the Track, so a crash after a send
        back was recorded re-runs the step with both its input and the findings.
        """
        value: Any = _NO_SIGNAL
        for e in events:
            if (e.event_type == "gate_decided" and e.payload.get("step") == step
                    and e.payload.get("outcome", "send_back") == "send_back"):
                value = e.payload.get("value")
        return value

    @staticmethod
    def _approved(events: tuple[TrackEvent, ...], step: str) -> Mapping[str, Any] | None:
        """The escalation of a step that was approved and is not yet completed, or ``None``.

        An approval completes the step with the envelope the approver saw, so
        a crash between recording the approval and completing the step completes
        it on recovery rather than running it again. An escalation recorded before
        Gates has no envelope to complete from, so its approval re-runs the step.
        """
        escalated: Mapping[str, Any] | None = None
        approved: Mapping[str, Any] | None = None
        for e in events:
            if e.payload.get("step") != step:
                continue
            if e.event_type == "gate_escalated":
                escalated, approved = e.payload, None
            elif e.event_type == "gate_decided":
                approved_now = e.payload.get("outcome") == "approve" and escalated is not None
                approved = escalated if approved_now and escalated.get("envelope") is not None else None
            elif e.event_type == "step_completed":
                approved = None
        return approved

    @staticmethod
    def _open_escalation(events: tuple[TrackEvent, ...], run: Run | None) -> dict[str, Any] | None:
        if run is None or run.state is not RunState.WAITING_AT_GATE:
            return None
        return _escalation(next(
            e.payload for e in reversed(events)
            if e.event_type == "gate_escalated" and e.payload.get("escalation") == run.open_escalation
        ))

    def open_escalation(self, run_id: str) -> Mapping[str, Any] | None:
        """What a run waiting at a Gate waits on — the escalation id, the envelope, who may decide — or ``None``."""
        events = self._read(run_id)
        return self._open_escalation(events, Run.replay(events))

    def submit(self, op: OpDefinition) -> RunState:
        names = [step.name for step in op.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"Op {op.run_id!r} has duplicate step names: {names}")
        existing = self._read(op.run_id)
        written: list[TrackEvent] = []
        if not existing:
            self._record(op.run_id, Run.submit(op.run_id, _serialize_op(op)), into=written)
        else:
            if _canonical_op(self._submitted_definition(op.run_id, existing)) != _canonical_op(op):
                raise ValueError(f"run {op.run_id!r} was submitted with a different Op")
            run = Run.replay(existing)
            if run is not None and run.state.ended:
                # A finished run is immutable: re-submitting must not silently
                # re-drive steps (and repeat side effects). Re-running a failed run
                # is a deliberate act — call retry().
                return run.state
            if run is not None and run.state is RunState.WAITING_AT_GATE:
                # A run waiting at a Gate resumes only through decide(), which
                # carries the decision. Re-submitting must not re-invoke the gated
                # step behind the gate's back with its original input. A decision
                # already recorded moved the run back to RUNNING, so a crash between
                # recording it and advancing resumes below, like a mid-step crash.
                return run.state
        return self._advance(op, (*existing, *written))

    def retry(self, run_id: str) -> RunState:
        """Re-drive an unsuccessfully-ended run from its first incomplete step.

        Retry is for failed runs. Exhausted duration/token/cost budgets are not
        reset — the run machine allows it as a new budget epoch, which #4 builds —
        so start a new run instead. A completed run has no incomplete steps, so
        retrying it would only append a spurious `completed`; that is rejected
        (re-running finished work is a new Op, with its own run id). Unlike a
        crash-recovery resume (which reuses the same idempotency key so a durable
        worker can dedupe), a retry of a failed run records a ``retry_requested``
        marker that advances the per-step attempt, so each step gets a fresh key —
        the caller is asking for the work to run again.
        """
        events = self._read(run_id)
        run = Run.replay(events)
        state = None if run is None else run.state
        if run is None or not run.state.ended:
            raise ValueError(f"run {run_id!r} is not terminal (status={state}); nothing to retry")
        if state is RunState.BUDGET_EXCEEDED:
            # The run machine allows it as a new budget epoch; the engine does not until #4 builds epochs.
            raise ValueError(f"run {run_id!r} exhausted its budget; start a new run instead")
        if not RUN.accepts(state, "retry"):
            done = "completed" if state is RunState.COMPLETED else f"was {state.value}"
            raise ValueError(f"run {run_id!r} {done}; nothing to retry (start a new run instead)")
        op = self._submitted_definition(run_id, events)
        written: list[TrackEvent] = []
        self._record(run_id, run.retry(), into=written)
        return self._advance(op, (*events, *written))

    # --- advancing a run ------------------------------------------------------------------------

    def _advance(self, op: OpDefinition, events: tuple[TrackEvent, ...] | None = None) -> RunState:
        # The caller passes the Track it has already read, so one call reads it once.
        now = _Pass(self, op.run_id, self._read(op.run_id) if events is None else events)
        run = Run.replay(now.history)
        if run.state is RunState.SUBMITTED:
            run = now.record(run.pickup())
        completed = self._completed_steps(now.history)
        retries = self._retry_count(now.history)
        spent_on: str | None = None
        try:
            tracker = self.budget_tracker(now.history)
        except UsageUnavailable as exc:
            run = now.record(run.fail(error="UsageUnavailable", reason=str(exc)))
            return run.state
        for step in op.steps:
            if step.name in completed:
                continue
            # The last step this call reaches names the budget stop, if one lands at the end.
            spent_on = step.name
            approved = self._approved(now.history, step.name)
            if approved is not None:
                # The approver saw this envelope, so it is the step's result; the step does not run again.
                self.complete_approved(now, step, approved)
                continue
            if tracker is not None:
                try:
                    tracker.check()
                except BudgetExceeded as exc:
                    return self.stop_for_budget(run, step.name, exc)
            attempt = self.resolve(now, op, step, run, retries)
            self._run_attempt(now, op, attempt)
            if attempt.teardown_error is not None or attempt.outcome[0] != "ok":
                return self.fail(now, run, attempt)
            budget_stop = None
            if tracker is not None:
                usage = attempt.usage or {}
                try:
                    tracker.consume(tokens=usage.get("tokens", 0), cost=usage.get("cost", 0.0))
                except BudgetExceeded as exc:
                    budget_stop = exc
            envelope = attempt.outcome[1]
            verdict, why = self.evaluate_gate(step, envelope)
            if verdict is GateOutcome.ESCALATE:
                # A budget stop never discards a result that was paid for: the escalation
                # is recorded, and the stop lands at the next boundary — the end of the
                # run included — by when no further work has been spent.
                return self.escalate(now, op, run, attempt, envelope, why)
            self.complete(now, op, attempt, envelope)
            if budget_stop is not None:
                return self.stop_for_budget(run, step.name, budget_stop)
        if tracker is not None and spent_on is not None:
            # The end of a run is a boundary too: one that overspent on its last step
            # stops here instead of completing over its budget.
            try:
                tracker.check()
            except BudgetExceeded as exc:
                return self.stop_for_budget(run, spent_on, exc)
        run = now.record(run.complete())
        return run.state

    def _run_attempt(self, now: _Pass, op: OpDefinition, attempt: Attempt) -> None:
        """Materialize, interact and read the envelope, then tear the worker down whatever happened.

        All three are inside failure handling so any infra error becomes a durable
        `failed` event (never a non-terminal run); teardown is best-effort in `finally`.
        """
        step = attempt.step
        try:
            self.materialize(now, op, attempt)
            self.interact(now, attempt)
            self.read_envelope(now, attempt)
        except UsageUnavailable as exc:
            # Persist unknown accounting so recovery/retry cannot forget it.
            now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            attempt.failure_reason = _bound(exc)
            attempt.outcome = ("broken", "UsageUnavailable")
        except EnvelopeInvalid as exc:
            # Not the seam's envelope, so whatever the worker spent is unknown too,
            # unless it was a valid envelope but for its payload.
            if not attempt.accounted:
                now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            attempt.failure_reason = _bound(exc)
            attempt.outcome = ("broken", "EnvelopeInvalid")
        except Exception as exc:  # noqa: BLE001 - any materialize/interact failure is durable-failed
            if attempt.invoked:
                # A failed request may have spent resources before failing.
                now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            # The class names the failure; its message, bounded, says what happened.
            attempt.failure_reason = _bound(exc) or None
            attempt.outcome = ("broken", type(exc).__name__)
        finally:
            if attempt.worker is not None:
                attempt.teardown_error = self.teardown(op.run_id, attempt)

    # --- the step functions ---------------------------------------------------------------------

    @step_function
    def resolve(self, now: _Pass, op: OpDefinition, step: OpStep, run: Run, retries: int) -> Attempt:
        """Name the attempt this step runs as, and record that it started.

        attempt = prior escalations (revisions) + explicit retries, NOT the
        step_started count: a crash before the outcome is recorded re-runs with
        the SAME instance/key (a durable worker can dedupe the replay), while an
        explicit retry() bumps the attempt so it re-runs under a fresh one.
        `instance` identifies the materialization (so distinct steps and attempts
        never reuse a still-terminating worker name); `key` is the same identity,
        run-scoped, handed to the worker for dedupe.
        """
        number = run.escalations.get(step.name, 0) + retries
        instance = f"{_key_component(step.name)}:{number}"
        attempt = Attempt(step=step, number=number, instance=instance, key=f"{_key_component(op.run_id)}:{instance}")
        now.append("step_started", {"step": step.name, "cog": step.cog, "digest": step.digest, "attempt": number})
        return attempt

    @step_function
    def materialize(self, now: _Pass, op: OpDefinition, attempt: Attempt) -> None:
        """Bring up the attempt's worker, and move its machine to ready."""
        step = attempt.step
        attempt.worker = self.executor.materialize(step.cog, op.run_id, attempt.instance)
        attempt.cog_worker = now.record(Worker.materialize(step.cog, step=step.name, digest=step.digest))
        attempt.cog_worker = now.record(attempt.cog_worker.ready())

    @step_function
    def interact(self, now: _Pass, attempt: Attempt) -> None:
        """Invoke the step's entry point with its input, and the findings of a send back if there was one."""
        step = attempt.step
        attempt.cog_worker = now.record(attempt.cog_worker.invoke(entry_point=step.entry_point, step=step.name))
        signal_value = self._signal_for(now.history, step.name)
        feedback = {} if signal_value is _NO_SIGNAL else {"signal": signal_value}
        attempt.invoked = True
        attempt.result = attempt.worker.interact(step.entry_point, step.input, idempotency_key=attempt.key,
                                                 **feedback)

    @step_function
    def read_envelope(self, now: _Pass, attempt: Attempt) -> None:
        """Check the answer is an envelope with a JSON payload, and account for what it spent."""
        result = attempt.result
        if not isinstance(result, ResultEnvelope):
            raise EnvelopeInvalid("interact() must return a ResultEnvelope")
        not_json = None
        if result.ok:
            try:
                attempt.rendered = _render(result.payload)
            except EnvelopeInvalid as exc:
                not_json = exc
        attempt.answered = not_json is None
        # ok with problems is not a failure: the step's Gate decides what
        # the problems mean. ok: false is one.
        attempt.outcome = ("ok", result) if result.ok else ("error", result)
        attempt.usage = _validate_usage(result.usage, self.budget)
        now.append("interaction_usage", {"step": attempt.step.name, "attempt": attempt.number, "usage": attempt.usage})
        attempt.accounted = True
        if not_json is not None:
            # The worker spent what it reported, so that is counted; its
            # answer is still not a valid envelope, and fails the step.
            raise not_json

    @step_function
    def teardown(self, run_id: str, attempt: Attempt) -> str | None:
        """Tear a step's worker down, moving it through its machine; the executor's error if teardown failed.

        A worker that answered — with an envelope, ok or not — goes IDLE and
        is torn down as a one-shot, and a teardown that fails is its machine's
        `teardown_failed`. One that did not answer has failed: the executor reclaims
        what is left of it, and if that fails the run's `failed` record says so.
        ``TORN_DOWN`` is never recorded, so the runner does not move the worker there.
        """
        cog_worker = attempt.cog_worker
        tearing_down = attempt.answered and cog_worker is not None
        if tearing_down:
            cog_worker = self._record(run_id, cog_worker.envelope_returned())
            cog_worker = self._record(run_id, cog_worker.tear_down(reason="one_shot"))
        elif cog_worker is not None:
            cog_worker = self._record(run_id, cog_worker.fail(error=str(attempt.outcome[1])))
        try:
            self.executor.teardown(attempt.worker)
        except Exception as exc:  # noqa: BLE001 - never crash on cleanup
            if tearing_down:
                self._record(run_id, cog_worker.fail(error=type(exc).__name__))
            return type(exc).__name__
        return None

    @step_function
    def evaluate_gate(self, step: OpStep, envelope: ResultEnvelope) -> tuple[GateOutcome, str | None]:
        """What the step's Gate decides about its envelope, and why."""
        return step.gate.evaluate(envelope)

    @step_function
    def complete(self, now: _Pass, op: OpDefinition, attempt: Attempt, envelope: ResultEnvelope) -> None:
        """Record the step's result: inline, or by reference above the threshold."""
        result = self._result(op.run_id, attempt.key, envelope.payload, attempt.rendered)
        now.append("step_completed", self._step_completed(attempt.step, attempt.number, envelope, attempt.usage,
                                                          result))

    @step_function
    def escalate(self, now: _Pass, op: OpDefinition, run: Run, attempt: Attempt, envelope: ResultEnvelope,
                 why: str | None) -> RunState:
        """Hold the run at the step's Gate, with the result a person decides on.

        A large result is kept once, by reference, like a completed one: the
        escalation holds the envelope without it, and its digest, so what a
        decision names still identifies the result.
        """
        step = attempt.step
        result = self._result(op.run_id, attempt.key, envelope.payload, attempt.rendered)
        shown = envelope.to_dict()
        if "payload_ref" in result:
            shown["payload"] = None
            shown["payload_digest"] = _payload_digest(envelope.payload)
        details = {
            "attempt": attempt.number, "envelope": shown, **result, "usage": attempt.usage,
            "approvers": list(step.gate.deciders), "gate": step.gate.escalate,
        }
        eid = escalation_id(op.run_id, step.name, attempt.number, envelope)
        run = now.record(run.escalate(step=step.name, reason=why, escalation=eid, details=details))
        return run.state

    @step_function
    def fail(self, now: _Pass, run: Run, attempt: Attempt) -> RunState:
        """Record a step that did not produce a result, and end the run ``failed``."""
        step, key, number = attempt.step, attempt.key, attempt.number
        kind, detail = attempt.outcome
        teardown_error = attempt.teardown_error
        if teardown_error is not None:
            # A worker we couldn't tear down may keep running/serving — that is
            # a leak, not success. Fail the run so it is visible; durable
            # cleanup-retry lands with the crash-safe engine backing (#1).
            # The step's own failure, when it had one, is what step_failed
            # records; the teardown that also failed is recorded beside it.
            if kind == "broken":
                own = self._step_failed(step, number, key, detail, attempt.failure_reason)
            elif kind == "error":
                own = self._step_failed(step, number, key, detail.error.code, detail.error.detail, detail)
            else:
                own = self._step_failed(step, number, key, "TeardownFailed",
                                        f"{teardown_error}: the worker could not be torn down")
            now.append("step_failed", {**own, "teardown_error": teardown_error})
            # A failed worker's own error is kept beside it, since its teardown is not a worker move.
            details = None if attempt.answered else {"worker_error": str(detail)}
            failed = run.fail(step=step.name, error="TeardownFailed", reason=_bound(teardown_error),
                              details=details)
            run = now.record(failed)
            return run.state
        if kind == "broken":
            now.append("step_failed", self._step_failed(step, number, key, detail, attempt.failure_reason))
            run = now.record(run.fail(step=step.name, error=detail, reason=_bound(attempt.failure_reason)))
            return run.state
        # The worker answered, and said no. The code is what a client acts
        # on, so it is the event's error, verbatim; the detail is the reason.
        now.append("step_failed", self._step_failed(step, number, key, detail.error.code, detail.error.detail,
                                                     detail))
        failed = run.fail(step=step.name, error=detail.error.code, reason=_bound(detail.error.detail),
                          details=_recorded(detail))
        run = now.record(failed)
        return run.state

    @step_function
    def complete_approved(self, now: _Pass, step: OpStep, approved: Mapping[str, Any]) -> None:
        """Complete a step whose escalated result was approved, from the envelope its approver saw."""
        envelope = ResultEnvelope.parse(approved["envelope"])
        kept = approved.get("payload_ref")
        result = {"payload_ref": kept} if kept else {"payload": envelope.payload}
        now.append("step_completed", self._step_completed(step, approved.get("attempt", 0), envelope,
                                                          approved.get("usage"), result, approved["escalation"]))

    @step_function
    def stop_for_budget(self, run: Run, step: str, exc: BudgetExceeded) -> RunState:
        """Stop the run at a boundary its budget has passed; a duration stop keeps the Track's `timed_out` event."""
        run = self._record(run.run_id, run.exhaust_budget(dimension=exc.dimension, step=step, reason=str(exc)))
        return run.state

    def decide(
        self, run_id: str, *, escalation: str | None, actor: str, outcome: str,
        findings: Sequence[Any] | None = (),
    ) -> RunState:
        """Answer the escalation a run waits on: ``approve``, ``reject`` or ``send_back``.

        The decision names the escalation it answers; one naming an escalation
        that is no longer open raises ``StaleEscalation`` and changes nothing.
        Approve completes the step with the envelope the approver saw, reject
        ends the run ``REJECTED``, and send back re-runs the step with the
        findings as its signal, up to ``max_revisions`` revisions. The decision is
        recorded before the run advances, so a crash after it resumes from it.
        Who may decide is the run API's to check (#103); the engine records who did.
        """
        if not actor:
            raise ValueError("a decision names its actor")
        if findings is not None and (isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence)):
            # One string, a mapping or a set is not a sequence of findings: listing it would
            # record its characters, its keys, or an arbitrary order.
            raise ValueError("findings are a sequence of findings, not one string, mapping or set")
        events = self._read(run_id)
        run = Run.replay(events)
        if run is None or run.state is not RunState.WAITING_AT_GATE:
            raise ValueError(f"run {run_id!r} is not waiting at a Gate")
        waiting_on = self._open_escalation(events, run)
        if outcome == "approve" and waiting_on is not None and waiting_on["envelope"] is None:
            # An escalation recorded before Gates holds no result, so there is nothing to
            # accept as it is; a send back asks for the work again, a reject ends the run.
            raise ValueError(f"run {run_id!r} escalated before Gates and has no recorded result to approve; "
                             f"send it back or reject it")
        op = self._submitted_definition(run_id, events)
        decided_on = None if waiting_on is None or waiting_on["envelope"] is None \
            else envelope_digest(waiting_on["envelope"])
        decision = run.decide(outcome=outcome, escalation=escalation, findings=list(findings or ()), actor=actor,
                              revise_limit=self.max_revisions, envelope_digest=decided_on)
        written: list[TrackEvent] = []
        run = self._record(run_id, decision, into=written)
        if run.state is not RunState.RUNNING:
            return run.state
        return self._advance(op, (*events, *written))
