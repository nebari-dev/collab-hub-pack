"""What a client asks of a run, written to the Track, and what the Track says of it.

The process that accepts runs is not the one that advances them (ADR-0002 D4).
The API writes intent here — a submission, a request to cancel — and reads a
run's status from the Track; it constructs no executor and never calls the
controller. The controller (``controller.py``) finds the intent on the Track
and acts on it.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def submit(track: TrackStore, op: OpDefinition, *, by: Mapping[str, Any]) -> None:
    """Record that ``by`` submitted an Op. Nothing runs here: a controller picks the run up.

    A run id submitted twice is refused by the Track (``OneSubmissionPerRun``).
    """
    for record in Run.submit(op.run_id, _serialize_op(op), by=by).records:
        _append(track, op.run_id, record.event_type, record.payload)


def request_cancel(track: TrackStore, run_id: str, *, actor: str) -> RunView:
    """Record that ``actor`` asked for a run to be cancelled; the controller tears it down and ends it.

    ``LookupError`` for a run never submitted, :class:`RunEnded` for one that
    has ended. Asking twice records the request once.
    """
    view = describe(track, run_id)
    if view is None:
        raise LookupError(f"no run {run_id!r} on the Track")
    if view.state.ended:
        raise RunEnded(view.state, CANCEL_REQUESTED, f"the run has ended {view.status}")
    if view.cancel_requested_by is None:
        _append(track, run_id, CANCEL_REQUESTED, {"actor": actor})
        return describe(track, run_id)
    return view


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
    cancel_requested_by = error = reason = None
    in_flight: set[str] = set()
    for event in events:
        payload = event.payload
        step = steps.get(payload.get("step")) if isinstance(payload.get("step"), str) else None
        if event.event_type == CANCEL_REQUESTED:
            cancel_requested_by = cancel_requested_by or payload.get("actor")
        elif event.event_type in ("failed", "budget_exceeded"):
            error, reason = payload.get("error") or event.event_type, payload.get("reason")
        elif event.event_type == "retry_requested":
            error = reason = None
        if step is None:
            continue
        if event.event_type == "step_started":
            step.update(state="running", attempt=payload.get("attempt"), error=None)
            in_flight.add(payload["step"])
            continue
        if event.event_type == "step_completed":
            step.update(state="completed", output=payload.get("payload"), output_ref=payload.get("payload_ref"))
        elif event.event_type == "step_failed":
            step.update(state="failed", error=payload.get("error"))
        elif event.event_type == "gate_escalated":
            step.update(state="waiting_at_gate")
        else:
            continue
        in_flight.discard(payload["step"])
    if run.state.ended:
        # A step still in flight when its run ended did not finish: it ended with the run.
        for name in in_flight:
            steps[name]["state"] = run.state.value
    return RunView(
        run_id=run_id, state=run.state, op=op,
        steps=tuple(StepView(name=step.name, cog=step.cog, **steps[step.name]) for step in op.steps),
        submitted_by=dict(submission.payload.get("submitted_by") or {}),
        submitted_at=submission.occurred_at, updated_at=events[-1].occurred_at,
        cancel_requested_by=cancel_requested_by, error=error, reason=reason,
    )


def list_runs(track: TrackStore) -> tuple[RunView, ...]:
    """Every run on the Track, newest submission first."""
    views = [view for view in (describe(track, run_id) for run_id in track.run_ids()) if view is not None]
    return tuple(sorted(views, key=lambda view: view.submitted_at, reverse=True))
