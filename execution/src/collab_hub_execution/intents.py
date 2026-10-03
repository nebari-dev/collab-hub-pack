"""What a client asks of a run, written to the Track, and what the Track says of it.

The process that accepts runs is not the one that advances them (ADR-0002 D4).
The API writes intent here — a submission, a request to cancel, a turn — and reads a
run's status from the Track; it constructs no executor and never calls the
controller. The controller (``controller.py``) finds the intent on the Track
and acts on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ops import OpDefinition, _deserialize_op, _serialize_op
from .states import InvalidTransition, Run, RunState
from .track import SCHEMA_VERSION, TrackEvent, TrackStore, upgrade

CANCEL_REQUESTED = "cancel_requested"


class RunEnded(InvalidTransition):
    """A request that needs a run still advancing, made of one that has ended."""


def _append(track: TrackStore, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    track.append(TrackEvent(run_id=run_id, event_type=event_type, payload=dict(payload), schema=SCHEMA_VERSION))


def submit(track: TrackStore, op: OpDefinition, *, by: Mapping[str, Any], name: str | None = None) -> None:
    """Record that ``by`` submitted an Op. Nothing runs here: a controller picks the run up.

    A run id submitted twice is refused by the Track (``OneSubmissionPerRun``).
    """
    for record in Run.submit(op.run_id, _serialize_op(op), by=by, name=name).records:
        _append(track, op.run_id, record.event_type, record.payload)


def cancel_request(events: Iterable[TrackEvent]) -> str | None:
    """Who asked for the run to be cancelled, as it stands now; ``None`` when nobody has.

    A request belongs to the attempt it was made of: one that a run outlived,
    because it ended on its own first and was then retried, does not cancel the
    retry.
    """
    actor = None
    for event in events:
        if event.event_type == CANCEL_REQUESTED:
            actor = actor or event.payload.get("actor")
        elif event.event_type == "retry_requested":
            actor = None
    return actor


def request_cancel(track: TrackStore, run_id: str, *, actor: str) -> RunView:
    """Record that ``actor`` asked for a run to be cancelled; the controller tears it down and ends it.

    ``LookupError`` for a run never submitted, :class:`RunEnded` for one that
    has ended. Asking twice records the request once. The request is appended
    only if, as it lands, the run has not ended and holds no request yet: the
    check and the append are one step on the Track, so a run that ends, or a
    second request that arrives, in between is seen.
    """
    def still_wanted(events: tuple[TrackEvent, ...]) -> bool:
        events = tuple(upgrade(event) for event in events)
        run = Run.replay(events)
        return run is not None and not run.state.ended and cancel_request(events) is None

    request = TrackEvent(run_id=run_id, event_type=CANCEL_REQUESTED, payload={"actor": actor}, schema=SCHEMA_VERSION)
    recorded = track.append_if(request, still_wanted)
    view = describe(track, run_id)
    if view is None:
        raise LookupError(f"no run {run_id!r} on the Track")
    if recorded is None and view.state.ended:
        raise RunEnded(view.state, CANCEL_REQUESTED, f"the run has ended {view.status}")
    return view  # recorded now, or a request already stood


@dataclass(frozen=True, slots=True)
class StepView:
    name: str
    cog: str
    state: str
    """``pending``, ``running``, ``completed``, ``failed`` or ``waiting_at_gate``."""
    attempt: int | None = None
    error: str | None = None
    output: Any = None
    """What the step's Cog answered with, when the step completed and the Track holds it inline."""
    output_ref: str | None = None
    """Where a completed step's output is kept, when it was too large for the Track to hold inline."""


@dataclass(frozen=True, slots=True)
class RunView:
    """A run as its Track tells it: the status, each step, who submitted it, and when."""

    run_id: str
    state: RunState
    op: OpDefinition
    steps: tuple[StepView, ...]
    submitted_by: Mapping[str, Any]
    submitted_at: datetime
    updated_at: datetime
    cancel_requested_by: str | None = None
    error: str | None = None
    reason: str | None = None
    name: str | None = None
    """What the client called the run when it submitted it, if anything: a label, never an id."""

    @property
    def status(self) -> str:
        return self.state.name


def describe(track: TrackStore, run_id: str) -> RunView | None:
    """The run's view, replayed from its Track; ``None`` for a run never submitted."""
    events = tuple(upgrade(event) for event in track.replay(run_id))
    run = Run.replay(events)
    if run is None:
        return None
    submission = next(event for event in events if event.event_type == "op_submitted")
    op = _deserialize_op(submission.payload["op"])
    steps: dict[str, dict[str, Any]] = {step.name: {"state": "pending"} for step in op.steps}
    error = reason = None
    cancel_requested_by = cancel_request(events)
    open_steps: set[str] = set()  # started, and neither completed nor failed: running, or waiting at a Gate
    for event in events:
        payload = event.payload
        step = steps.get(payload.get("step")) if isinstance(payload.get("step"), str) else None
        if event.event_type in ("failed", "budget_exceeded"):
            error, reason = payload.get("error") or event.event_type, payload.get("reason")
        elif event.event_type == "retry_requested":
            error = reason = None
        if step is None:
            continue
        if event.event_type == "step_started":
            step.update(state="running", attempt=payload.get("attempt"), error=None)
            open_steps.add(payload["step"])
        elif event.event_type == "gate_escalated":
            step.update(state="waiting_at_gate")
            open_steps.add(payload["step"])
        elif event.event_type == "step_completed":
            step.update(state="completed", output=payload.get("payload"), output_ref=payload.get("payload_ref"))
            open_steps.discard(payload["step"])
        elif event.event_type == "step_failed":
            step.update(state="failed", error=payload.get("error"))
            open_steps.discard(payload["step"])
    if run.state.ended:
        # A step that was running, or waiting at its Gate, when its run ended did not finish: it
        # ended with the run, cancelled, rejected or interrupted as the run was.
        for name in open_steps:
            steps[name]["state"] = run.state.value
    return RunView(
        run_id=run_id, state=run.state, op=op,
        steps=tuple(StepView(name=step.name, cog=step.cog, **steps[step.name]) for step in op.steps),
        submitted_by=dict(submission.payload.get("submitted_by") or {}), name=submission.payload.get("name"),
        submitted_at=submission.occurred_at, updated_at=events[-1].occurred_at,
        cancel_requested_by=cancel_requested_by, error=error, reason=reason,
    )


def list_runs(track: TrackStore) -> tuple[RunView, ...]:
    """Every run on the Track, newest submission first."""
    views = [view for view in (describe(track, run_id) for run_id in track.run_ids()) if view is not None]
    return tuple(sorted(views, key=lambda view: view.submitted_at, reverse=True))


# --- turns: talking to a Cog while its step runs ------------------------------------------------
#
# A Cog whose entry point holds a session (``hello``'s ``session``) answers turns while its step
# is in flight. A client asks for a turn here; the controller delivers it to the run's live
# worker and records the answer. Every turn and its answer are on the Track, like the rest of
# the run.

TURN_REQUESTED, TURN_ANSWERED, TURN_FAILED = "turn_requested", "turn_answered", "turn_failed"
MAX_TURN_TEXT = 64 * 1024


@dataclass(frozen=True, slots=True)
class TurnView:
    turn: str
    text: str
    actor: str | None
    state: str
    """``pending`` until the worker answers, then ``answered`` or ``failed``."""
    answer: str | None = None
    error: str | None = None


def turns(events: Iterable[TrackEvent]) -> dict[str, TurnView]:
    """Every turn of a run, in the order it was asked, as its Track tells it."""
    events = tuple(upgrade(event) for event in events)
    asked: dict[str, TurnView] = {}
    for event in events:
        payload = event.payload
        turn = payload.get("turn")
        if event.event_type == TURN_REQUESTED:
            asked[turn] = TurnView(turn=turn, text=payload.get("text", ""), actor=payload.get("actor"),
                                   state="pending")
        elif event.event_type == TURN_ANSWERED and turn in asked:
            asked[turn] = _replace(asked[turn], state="answered", answer=payload.get("text"))
        elif event.event_type == TURN_FAILED and turn in asked:
            asked[turn] = _replace(asked[turn], state="failed", error=payload.get("error"))
    run = Run.replay(events)
    if run is not None and run.state.ended:
        # A turn still waiting when its run ended will never be answered.
        for turn, view in asked.items():
            if view.state == "pending":
                asked[turn] = _replace(view, state="failed", error=f"the run ended {run.state.name}")
    return asked


def _replace(view: TurnView, **changes: Any) -> TurnView:
    return TurnView(**{**{name: getattr(view, name) for name in TurnView.__slots__}, **changes})


def request_turn(track: TrackStore, run_id: str, *, text: str, actor: str) -> TurnView:
    """Ask a running Cog something. The controller delivers it to the run's worker, in order.

    ``LookupError`` for a run never submitted, :class:`RunEnded` for one that
    has ended. The request is written only if, as it lands, the run has not
    ended.
    """
    if len(text.encode()) > MAX_TURN_TEXT:
        raise ValueError(f"a turn's text is at most {MAX_TURN_TEXT} bytes")
    turn = uuid.uuid4().hex[:12]

    def advancing(events: tuple[TrackEvent, ...]) -> bool:
        run = Run.replay(tuple(upgrade(event) for event in events))
        return run is not None and not run.state.ended

    request = TrackEvent(run_id=run_id, event_type=TURN_REQUESTED,
                         payload={"turn": turn, "text": text, "actor": actor}, schema=SCHEMA_VERSION)
    recorded = track.append_if(request, advancing)
    if recorded is None:
        view = describe(track, run_id)
        if view is None:
            raise LookupError(f"no run {run_id!r} on the Track")
        raise RunEnded(view.state, TURN_REQUESTED, f"the run has ended {view.status}")
    return turns(track.replay(run_id))[turn]


def answer_turn(track: TrackStore, run_id: str, turn: str, *, text: str | None = None,
                error: str | None = None) -> None:
    """The controller's record of what became of a turn: the worker's answer, or why there is none."""
    if (text is None) == (error is None):
        raise ValueError("a turn is answered with a text or failed with an error")
    if text is not None:
        _append(track, run_id, TURN_ANSWERED, {"turn": turn, "text": text})
    else:
        _append(track, run_id, TURN_FAILED, {"turn": turn, "error": error})
