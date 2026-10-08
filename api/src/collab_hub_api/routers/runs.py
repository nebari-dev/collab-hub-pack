"""The run API's first slice: launch a Cog, see what is running, stop it.

Behind the ``cog_runs`` feature flag, and mounted only when it is on. The API
records intent on the Track and reads a run's status from it; a run controller,
a separate process over the same Track, advances the runs (ADR-0002 D4). The
API constructs no executor and never calls the controller.

- ``POST /v1/runs`` -- submit an Op: steps naming a Cog package, an entry
  point, an input and a Gate. A step whose ``cog`` is not a package the
  controller can launch is refused with 422, naming the packages it can.
- ``GET /v1/runs``, ``GET /v1/runs/{id}`` -- the caller's organization's runs,
  status and each step's state replayed from the Track. The single run also
  carries each completed step's output.
- ``POST /v1/runs/{id}/cancel`` -- record a request to cancel; the controller
  tears the worker down and the run ends ``CANCELLED``. A run that has ended
  answers 409, naming its status.
- ``POST /v1/runs/{id}/turns``, ``GET /v1/runs/{id}/turns/{turn}`` -- talk to a
  Cog whose step holds a session: the API records the turn, the controller
  delivers it to the run's live worker and records the answer, which the client
  reads back. Every turn and its answer are on the Track.
- ``GET /v1/runs/launchable`` -- the Cog packages a step may name.

Runs are scoped to the organization that submitted them: another
organization's run is a 404, never a 403. Events, payloads, decisions and
retry are later phases of the plan.

The execution package is imported here and nowhere else in the API, and only
when the flag is on, so an image that does not ship it starts as before.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from collab_hub_execution import intents
from collab_hub_execution.gates import POLICIES, Gate
from collab_hub_execution.locations.packages import DirectoryPackageSource, PackageNotFound, PackageRefused
from collab_hub_execution.ops import OpDefinition, OpStep
from collab_hub_execution.track import SqliteTrackStore, TrackStore
from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import RunsConfig
from ..frames.auth import AuthContext, get_auth_context
from .frames import error_response

router = APIRouter(prefix="/runs", tags=["runs"])

RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class RunService:
    """What the run routes need: the Track, the packages that can be launched, and what runs them."""

    def __init__(self, track: TrackStore, packages: DirectoryPackageSource | None, *, backend: str,
                 location: str) -> None:
        self.track = track
        self.views = intents.RunViews(track)  # read incrementally: a page costs what changed, not all history
        self.packages = packages
        self.backend = backend
        self.location = location

    def launchable(self) -> tuple[str, ...]:
        return () if self.packages is None else self.packages.names()

    def refusal(self, cog: str) -> str | None:
        """Why the controller could not launch ``cog``; ``None`` when it can."""
        if self.packages is None:
            return "no package directory is configured"
        try:
            self.packages.resolve(cog)
        except PackageRefused as exc:
            return str(exc)
        except PackageNotFound as exc:
            # The source's own message names the directories it looked in, which a client has no use for.
            return str(exc).split(" under ")[0]
        return None


def build_run_service(config: RunsConfig) -> RunService:
    """The run service of a deployment whose ``cog_runs`` flag is on."""
    if not config.track_path:
        raise RuntimeError(
            "the cog_runs feature needs runs.track_path (COLLAB_HUB_API__RUNS__TRACK_PATH): the Track file "
            "the run controller watches")
    SqliteTrackStore.ensure_schema(config.track_path)
    packages = DirectoryPackageSource(config.packages, config.allow) if config.packages else None
    return RunService(SqliteTrackStore(config.track_path), packages, backend=config.backend,
                      location=config.location)


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


class GateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalate: str = "error"
    approvers: list[str] = Field(default_factory=list)

    @field_validator("escalate")
    @classmethod
    def _a_policy(cls, value: str) -> str:
        if value not in POLICIES:
            raise ValueError(f"a Gate escalates one of {', '.join(POLICIES)}")
        return value


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    cog: str = Field(min_length=1, max_length=257)
    entry_point: str = Field(min_length=1, max_length=128)
    input: Any = None
    gate: GateRequest = Field(default_factory=GateRequest)


class RunRequest(BaseModel):
    """An Op to run: its steps, in order."""

    model_config = ConfigDict(extra="forbid")

    steps: list[StepRequest] = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    """What to call the run in listings, e.g. ``hermes-on-claude``. A label: runs are found by their id."""

    @field_validator("steps")
    @classmethod
    def _distinct_names(cls, steps: list[StepRequest]) -> list[StepRequest]:
        names = [step.name for step in steps]
        if len(set(names)) != len(names):
            raise ValueError("each step of an Op has its own name")
        return steps


class StepStatus(BaseModel):
    name: str
    cog: str
    entry_point: str
    state: Literal["pending", "running", "completed", "failed", "waiting_at_gate", "cancelled", "interrupted",
                   "budget_exceeded", "rejected"]
    attempt: int | None = None
    error: str | None = None
    output: Any = None
    """What a completed step's Cog answered with. Only on ``GET /v1/runs/{id}``, and only when the
    Track holds it inline: a larger one is named by ``output_ref`` until the payload route exists."""
    output_ref: str | None = None


class RunStatus(BaseModel):
    id: str
    status: str
    """The run's state, as the Track tells it: SUBMITTED, RUNNING, WAITING_AT_GATE, COMPLETED, FAILED,
    REJECTED, CANCELLED, INTERRUPTED or BUDGET_EXCEEDED."""
    ended: bool
    steps: list[StepStatus]
    submitted_by: str | None
    name: str | None = None
    """What the run was called when it was launched, if anything."""
    submitted_by_name: str | None = None
    """Who submitted the run, for showing: their name or address when the sign-in carried one. Never
    compared with anything; ``submitted_by`` is the principal."""
    submitted_at: str
    updated_at: str
    cancel_requested_by: str | None = None
    error: str | None = None
    reason: str | None = None
    backend: str
    """The durability backend the run is advanced on. ``none`` keeps nothing across a controller restart."""
    location: str
    """Where the run's workers are. A ``local`` worker shares the controller's host."""


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=intents.MAX_TURN_TEXT)


class TurnStatus(BaseModel):
    turn: str
    text: str
    state: Literal["pending", "answered", "failed"]
    """``pending`` until the run's worker answers."""
    answer: str | None = None
    error: str | None = None
    asked_by: str | None = None


class Launchable(BaseModel):
    items: list[str]
    """The Cog packages a step's ``cog`` may name."""


class RunPage(BaseModel):
    items: list[RunStatus]
    next_offset: int | None = None


def _status(view: intents.RunView, service: RunService, *, outputs: bool = False) -> RunStatus:
    entry_points = {step.name: step.entry_point for step in view.op.steps}
    return RunStatus(
        id=view.run_id, status=view.status, ended=view.state.ended,
        steps=[StepStatus(name=step.name, cog=step.cog, entry_point=entry_points[step.name], state=step.state,
                          attempt=step.attempt, error=step.error,
                          output=step.output if outputs else None, output_ref=step.output_ref if outputs else None)
               for step in view.steps],
        name=view.name, submitted_by=view.submitted_by.get("user"), submitted_by_name=view.submitted_by.get("name"),
        submitted_at=view.submitted_at.isoformat(), updated_at=view.updated_at.isoformat(),
        cancel_requested_by=view.cancel_requested_by, error=view.error, reason=view.reason,
        backend=service.backend, location=service.location,
    )


def _not_found(run_id: str) -> JSONResponse:
    return error_response(status.HTTP_404_NOT_FOUND, "run_not_found", f"No run {run_id}")


def _visible(service: RunService, run_id: str, auth: AuthContext) -> intents.RunView | None:
    """The run, when the caller's organization submitted it; a run that cannot be read is not shown."""
    try:
        view = service.views.view(run_id)
    except intents.RunUnreadable:
        return None
    if view is None or view.submitted_by.get("org_id") != auth.org_id:
        return None
    return view


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
ServiceDep = Annotated[RunService, Depends(get_run_service)]
RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RunStatus)
def submit_run(body: RunRequest, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    refused = {step.cog: reason for step in body.steps if (reason := service.refusal(step.cog)) is not None}
    if refused:
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "cog_not_launchable",
            f"Cannot launch {', '.join(sorted(refused))}; the Cogs this hub launches are: "
            f"{', '.join(service.launchable()) or 'none'}",
            {"refused": refused, "launchable": list(service.launchable())})
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    op = OpDefinition(run_id, tuple(
        OpStep(name=step.name, cog=step.cog, entry_point=step.entry_point, input=step.input,
               gate=Gate(escalate=step.gate.escalate, approvers=tuple(step.gate.approvers)))
        for step in body.steps))
    # `user` is the principal the run is scoped and checked by; `name` is only for showing who it was.
    by = {"user": auth.user, "org_id": auth.org_id, "workspace_id": auth.workspace_id}
    if auth.display.name or auth.display.email:
        by["name"] = auth.display.name or auth.display.email
    intents.submit(service.track, op, by=by, name=body.name)
    return _status(intents.describe(service.track, run_id), service)


@router.get("", response_model=RunPage)
def list_runs(
    auth: AuthDep, service: ServiceDep,
    run_status: Annotated[str | None, Query(alias="status", description="Only runs in this status.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    views = service.views.views(org_id=auth.org_id, status=run_status.upper() if run_status else None)
    page = views[offset:offset + limit]
    return RunPage(items=[_status(view, service) for view in page],
                   next_offset=offset + limit if offset + limit < len(views) else None)


@router.get("/launchable", response_model=Launchable)
def launchable(auth: AuthDep, service: ServiceDep) -> Launchable:
    return Launchable(items=list(service.launchable()))


@router.get("/{run_id}", response_model=RunStatus)
def get_run(run_id: RunId, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    view = _visible(service, run_id, auth)
    return _not_found(run_id) if view is None else _status(view, service, outputs=True)


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED, response_model=RunStatus)
def cancel_run(run_id: RunId, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    try:
        view = intents.request_cancel(service.track, run_id, actor=auth.user)
    except intents.RunEnded as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_ended",
                              f"Run {run_id} cannot be cancelled: {exc.reason}")
    return _status(view, service)


def _turn(view: intents.TurnView) -> TurnStatus:
    return TurnStatus(turn=view.turn, text=view.text, state=view.state, answer=view.answer, error=view.error,
                      asked_by=view.actor)


@router.post("/{run_id}/turns", status_code=status.HTTP_202_ACCEPTED, response_model=TurnStatus)
def ask_turn(run_id: RunId, body: TurnRequest, auth: AuthDep, service: ServiceDep) -> TurnStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    try:
        view = intents.request_turn(service.track, run_id, text=body.text, actor=auth.user)
    except intents.RunEnded as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_ended", f"Run {run_id} takes no turns: {exc.reason}")
    except ValueError as exc:
        # The limit is in bytes; the request model bounds characters, which a non-ASCII text can pass.
        return error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "turn_too_long", str(exc))
    return _turn(view)


@router.get("/{run_id}/turns/{turn}", response_model=TurnStatus)
def get_turn(run_id: RunId, turn: Annotated[str, Path(pattern=r"^[0-9a-f]{12}$")], auth: AuthDep,
             service: ServiceDep) -> TurnStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    view = service.views.turns(run_id).get(turn)
    if view is None:
        return error_response(status.HTTP_404_NOT_FOUND, "turn_not_found", f"No turn {turn} in run {run_id}")
    return _turn(view)
