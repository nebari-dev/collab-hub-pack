"""Durable multi-step Op orchestration behind a replaceable engine contract."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .envelope import EnvelopeInvalid, ResultEnvelope
from .lifecycle import BudgetExceeded, BudgetTracker, RunBudget
from .states import RUN, Run, RunState, Transition, Worker
from .track import TrackEvent, TrackStore

# Distinguishes "no external signal" (a fresh submit/retry) from a signal whose
# value is genuinely None (a human resuming a Gate with an empty decision). None
# alone is overloaded, so a paused step could not be resumed with a real None.
_NO_SIGNAL = object()


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
    for field in ("tokens", "cost"):
        if field not in usage:
            if budget is not None and getattr(budget, f"max_{field}") is not None:
                raise UsageUnavailable(f"missing {field} usage for configured budget")
            continue
        value = usage[field]
        valid = type(value) is int if field == "tokens" else type(value) in (int, float)
        if not valid or value < 0 or (type(value) is float and not math.isfinite(value)):
            raise UsageUnavailable(f"invalid {field} usage")
        if field == "cost":
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise UsageUnavailable("invalid cost usage")
    return {key: usage[key] for key in ("tokens", "cost") if key in usage} if raw is not None else None


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
    """A Cog's request for an external signal before continuing.

    Transitional. A pause is a Gate's decision, declared on the Op step, never
    something a Cog asks for; this leaves the protocol when step-declared Gates
    land (#99). Until then the reference worker's ``{"pause": true}`` answer is
    surfaced through it.
    """

    def __init__(self, reason: str, *, usage: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.usage = usage


class CogWorker(Protocol):
    def interact(
        self, entry_point: str, input: Any = None, idempotency_key: str | None = None,
        *, signal: Any = _NO_SIGNAL,
    ) -> ResultEnvelope:
        """Interact through a declared entry point.

        Return the result envelope (``envelope.py``): ``payload`` is the Cog's
        output and ``usage`` its accounting, never hidden inside the payload.
        A PauseRequest carries usage for the interaction that paused.

        ``idempotency_key`` is stable per (run, step, attempt): a crash-recovery
        re-drives the same incomplete step with the *same* key, and an explicit
        retry or resume after a pause uses a *new* key. ``input`` always contains
        the step's original input; ``signal`` carries external feedback separately
        and is omitted until supplied, including when its explicit value is None.
        A worker that persists results by key can turn the
        replay into a no-op — but that durability is the worker's to provide. The
        reference and Kubernetes workers here do NOT persist keys across pod
        replacement, so a replaced worker re-runs the side effect: execution is
        at-least-once across pod replacement. Crash-safe, exactly-once execution
        (a durable keyed claim) lands with the crash-safe engine backing tracked
        in #1.
        """


class CogExecutor(Protocol):
    def materialize(self, cog: str, run_id: str, instance: str = "") -> CogWorker:
        """Materialize one Cog worker for a run.

        ``instance`` uniquely and recovery-stably identifies this materialization
        within the run (the engine passes step name + attempt). An executor that
        names cluster resources should fold it in so distinct steps and successive
        attempts never reuse a name a prior teardown may still be terminating.
        """

    def teardown(self, worker: CogWorker) -> None:
        """Release a materialized worker."""


class WorkflowEngine(Protocol):
    """Experimental boundary used by callers, independent of engine choice."""

    def submit(self, op: OpDefinition) -> RunState:
        """Start or recover an Op."""

    def signal(self, run_id: str, value: Any = None) -> RunState:
        """Resume an Op waiting at a Gate with an external value."""

    def observe(self, run_id: str) -> RunState | None:
        """Return the run's state reconstructed from the Track; ``None`` if never submitted."""


class InMemoryCogExecutor(CogExecutor):
    """A fake executor for exercising orchestration without infrastructure.

    A handler returns a ``ResultEnvelope`` (or an envelope-shaped mapping, one
    with an ``envelope`` key) to report usage or problems. Any other value
    becomes the payload of a successful envelope with unknown usage, which
    cannot satisfy a configured spending limit.
    """

    def __init__(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self.handlers = handlers
        self.materialized: list[tuple[str, str]] = []
        self.torn_down: list[str] = []

    def materialize(self, cog: str, run_id: str, instance: str = "") -> CogWorker:
        self.materialized.append((run_id, cog))
        return _Worker(cog, self.handlers[cog])

    def teardown(self, worker: CogWorker) -> None:
        self.torn_down.append(worker.cog)  # type: ignore[attr-defined]


class _Worker:
    def __init__(self, cog: str, handler: Callable[..., Any]) -> None:
        self.cog = cog
        self.handler = handler

    def interact(
        self, entry_point: str, input: Any = None, idempotency_key: str | None = None,
        *, signal: Any = _NO_SIGNAL,
    ) -> ResultEnvelope:
        feedback = {} if signal is _NO_SIGNAL else {"signal": signal}
        result = self.handler(entry_point, input, **feedback)
        if isinstance(result, ResultEnvelope):
            return result
        if isinstance(result, Mapping) and "envelope" in result:
            return ResultEnvelope.parse(result)
        return ResultEnvelope.success(result)


class DurableWorkflowEngine(WorkflowEngine):
    """An engine whose recovery source is exclusively the Track.

    Experimental: interfaces may change. submit(), signal(), and retry() run
    synchronously until completion, pause, or failure. After a process restart,
    a caller must resubmit the same Op; no background recovery loop is provided.

    Single-owner by assumption: it holds no cross-replica lease, so the same run
    must not be advanced from two API replicas concurrently. Multi-replica
    single-owner execution (an advancement lease) is provided by the crash-safe
    engine backing tracked in #1; the Postgres Track's one-submission-per-run index
    guards only a duplicated *submission*, not concurrent *advancement*.

    Every change of a run's or a worker's state is a transition of its machine
    (``states/``): the engine asks, and writes the records the machine returns.
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

    def _record(self, run_id: str, transition: Transition[Any]) -> Any:
        """Write what a transition reports, and return the context after it."""
        for record in transition.records:
            self._append(run_id, record.event_type, **record.payload)
        return transition.after

    def observe(self, run_id: str) -> RunState | None:
        run = Run.replay(self.track.replay(run_id))
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

    def _budget_tracker(self, events: tuple[TrackEvent, ...]) -> BudgetTracker | None:
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
        """The latest durably-recorded signal value for a step, or ``_NO_SIGNAL``.

        Recovery re-reads the decision from the Track, so a crash after a signal
        was recorded resumes with both the original input and the signal.
        """
        value: Any = _NO_SIGNAL
        for e in events:
            if e.event_type == "signal_received" and e.payload.get("step") == step:
                value = e.payload.get("value")
        return value

    def submit(self, op: OpDefinition) -> RunState:
        names = [step.name for step in op.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"Op {op.run_id!r} has duplicate step names: {names}")
        existing = self.track.replay(op.run_id)
        if not existing:
            self._record(op.run_id, Run.submit(op.run_id, _serialize_op(op)))
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
                # A run waiting at a Gate resumes only through signal(), which
                # carries the decision. Re-submitting must not re-invoke the gated
                # step behind the gate's back with its original input. A decision
                # already recorded moved the run back to RUNNING, so a crash between
                # recording it and advancing resumes below, like a mid-step crash.
                return run.state
        return self._advance(op)

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
        events = self.track.replay(run_id)
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
        self._record(run_id, run.retry())
        return self._advance(op)

    def _advance(self, op: OpDefinition) -> RunState:
        events = self.track.replay(op.run_id)
        run = Run.replay(events)
        if run.state is RunState.SUBMITTED:
            run = self._record(op.run_id, run.pickup())
        completed = self._completed_steps(events)
        retries = self._retry_count(events)
        try:
            tracker = self._budget_tracker(events)
        except UsageUnavailable as exc:
            run = self._record(op.run_id, run.fail(error="UsageUnavailable", reason=str(exc)))
            return run.state
        for step in op.steps:
            if step.name in completed:
                continue
            if tracker is not None:
                try:
                    tracker.check()
                except BudgetExceeded as exc:
                    return self._stop_for_budget(run, step.name, exc)
            # attempt = prior pauses (revisions) + explicit retries, NOT the
            # step_started count: a crash before the outcome is recorded re-runs
            # with the SAME instance/key (a durable worker can dedupe the replay),
            # while an explicit retry() bumps the attempt so it re-runs under a fresh
            # one. `instance` identifies the materialization (so distinct steps and
            # attempts never reuse a still-terminating worker name); `key` is the
            # same identity, run-scoped, handed to the worker for dedupe.
            attempt = run.escalations.get(step.name, 0) + retries
            instance = f"{_key_component(step.name)}:{attempt}"
            key = f"{_key_component(op.run_id)}:{instance}"
            self._append(op.run_id, "step_started", step=step.name, cog=step.cog, digest=step.digest, attempt=attempt)
            worker = None
            cog_worker: Worker | None = None
            answered = False
            outcome: tuple[str, Any] = ("broken", "Unknown")
            teardown_error: str | None = None
            usage = None
            failure_reason = None
            invoked = False
            # Materialize, interact, and teardown are all inside failure handling
            # so any infra error becomes a durable `failed` event (never a
            # non-terminal run); teardown is best-effort in `finally`.
            try:
                worker = self.executor.materialize(step.cog, op.run_id, instance)
                cog_worker = self._record(op.run_id, Worker.materialize(step.cog, step=step.name, digest=step.digest))
                cog_worker = self._record(op.run_id, cog_worker.ready())
                cog_worker = self._record(op.run_id, cog_worker.invoke(entry_point=step.entry_point, step=step.name))
                signal_value = self._signal_for(events, step.name)
                feedback = {} if signal_value is _NO_SIGNAL else {"signal": signal_value}
                try:
                    invoked = True
                    result = worker.interact(step.entry_point, step.input, idempotency_key=key, **feedback)
                    if not isinstance(result, ResultEnvelope):
                        raise EnvelopeInvalid("interact() must return a ResultEnvelope")
                    answered = True
                    # ok with problems is not a failure: the step completes and a
                    # Gate decides what the problems mean. ok: false is one.
                    outcome = ("ok", result) if result.ok else ("error", result)
                    raw_usage = result.usage
                except PauseRequest as pause:
                    answered = True
                    outcome = ("pause", pause.reason)
                    raw_usage = pause.usage
                usage = _validate_usage(raw_usage, self.budget)
                self._append(op.run_id, "interaction_usage", step=step.name, attempt=attempt, usage=usage)
            except UsageUnavailable as exc:
                # Persist unknown accounting so recovery/retry cannot forget it.
                self._append(op.run_id, "interaction_usage", step=step.name, attempt=attempt, usage=None)
                failure_reason = str(exc)
                outcome = ("broken", "UsageUnavailable")
            except EnvelopeInvalid as exc:
                # Not the seam's envelope, so whatever the worker spent is unknown too.
                self._append(op.run_id, "interaction_usage", step=step.name, attempt=attempt, usage=None)
                failure_reason = str(exc)
                outcome = ("broken", "EnvelopeInvalid")
            except Exception as exc:  # noqa: BLE001 - any materialize/interact failure is durable-failed
                if invoked:
                    # A failed request may have spent resources before failing.
                    self._append(op.run_id, "interaction_usage", step=step.name, attempt=attempt, usage=None)
                outcome = ("broken", type(exc).__name__)
            finally:
                if worker is not None:
                    teardown_error = self._tear_down(op.run_id, worker, cog_worker, answered, outcome)

            if teardown_error is not None:
                # A worker we couldn't tear down may keep running/serving — that is
                # a leak, not success. Fail the run so it is visible; durable
                # cleanup-retry lands with the crash-safe engine backing (#1).
                # A failed worker's own error is kept beside it, since its teardown is not a worker move.
                details = None if answered else {"worker_error": str(outcome[1])}
                failed = run.fail(step=step.name, error="TeardownFailed", reason=teardown_error, details=details)
                run = self._record(op.run_id, failed)
                return run.state

            kind, detail = outcome
            if kind == "broken":
                run = self._record(op.run_id, run.fail(step=step.name, error=detail, reason=failure_reason))
                return run.state
            if kind == "error":
                # The worker answered, and said no. The code is what a client acts
                # on, so it is the event's error, verbatim; the detail is the reason.
                failed = run.fail(step=step.name, error=detail.error.code, reason=detail.error.detail,
                                  details=_recorded(detail))
                run = self._record(op.run_id, failed)
                return run.state
            budget_stop = None
            if tracker is not None:
                try:
                    tracker.consume(tokens=(usage or {}).get("tokens", 0), cost=(usage or {}).get("cost", 0.0))
                except BudgetExceeded as exc:
                    budget_stop = exc
            if kind == "pause":
                if budget_stop is not None:
                    return self._stop_for_budget(run, step.name, budget_stop)
                escalation = run.escalate(step=step.name, reason=detail, revise_limit=self.max_revisions)
                run = self._record(op.run_id, escalation)
                return run.state

            envelope = detail
            # `output` keeps the Track's current key; the versioned event schema
            # that renames it and stores large payloads by reference is #5.
            self._append(op.run_id, "step_completed", step=step.name, output=envelope.payload, usage=usage,
                         **_recorded(envelope))
            if budget_stop is not None:
                return self._stop_for_budget(run, step.name, budget_stop)
        run = self._record(op.run_id, run.complete())
        return run.state

    def _tear_down(
        self, run_id: str, worker: CogWorker, cog_worker: Worker | None, answered: bool, outcome: tuple[str, Any]
    ) -> str | None:
        """Tear a step's worker down, moving it through its machine; the executor's error if teardown failed.

        A worker that answered — an envelope, ok or not, or a pause — goes IDLE and
        is torn down as a one-shot, and a teardown that fails is its machine's
        `teardown_failed`. One that did not answer has failed: the executor reclaims
        what is left of it, and if that fails the run's `failed` record says so.
        ``TORN_DOWN`` is never recorded, so the engine does not move the worker there.
        """
        tearing_down = answered and cog_worker is not None
        if tearing_down:
            cog_worker = self._record(run_id, cog_worker.envelope_returned())
            cog_worker = self._record(run_id, cog_worker.tear_down(reason="one_shot"))
        elif cog_worker is not None:
            cog_worker = self._record(run_id, cog_worker.fail(error=str(outcome[1])))
        try:
            self.executor.teardown(worker)
        except Exception as exc:  # noqa: BLE001 - never crash on cleanup
            if tearing_down:
                self._record(run_id, cog_worker.fail(error=type(exc).__name__))
            return type(exc).__name__
        return None

    def _stop_for_budget(self, run: Run, step: str, exc: BudgetExceeded) -> RunState:
        """Record a budget stop; a duration stop keeps the Track's `timed_out` event."""
        run = self._record(run.run_id, run.exhaust_budget(dimension=exc.dimension, step=step, reason=str(exc)))
        return run.state

    def signal(self, run_id: str, value: Any = None) -> RunState:
        events = self.track.replay(run_id)
        run = Run.replay(events)
        if run is None or run.state is not RunState.WAITING_AT_GATE:
            raise ValueError(f"run {run_id!r} is not paused")
        op = self._submitted_definition(run_id, events)
        # Persist the decision before advancing so a crash mid-resume recovers the
        # signal from the Track alongside the original input. The value may
        # legitimately be None — see _NO_SIGNAL. #35's signal re-runs the paused
        # step with its value, which is a send back; it answers whichever
        # escalation is open, since escalation ids arrive with step Gates (#99).
        # It cannot say whether it approves, so the revise limit is not applied
        # here but when the step escalates again (_advance), as #35 applied it.
        self._record(run_id, run.decide(outcome="send_back", escalation=run.open_escalation, findings=value))
        return self._advance(op)


def _canonical_op(op: OpDefinition) -> str:
    """Compare definitions using the same JSON representation as durable storage."""
    return json.dumps(json.loads(json.dumps(_serialize_op(op))), sort_keys=True)


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
