from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..dependencies import get_task_store
from ..frames.auth import AuthContext, get_auth_context
from ..tasks.models import DeviceHeartbeat, TaskCreate, TaskPatch, TaskRunCreate, TaskRunPatch, TaskStoragePolicy
from ..tasks.store import InMemoryTaskStore, TaskConflictError, TaskNotFoundError

router = APIRouter(prefix="/tasks", tags=["tasks"])
devices_router = APIRouter(prefix="/task-devices", tags=["tasks"])
notifications_router = APIRouter(prefix="/task-notifications", tags=["tasks"])
runs_router = APIRouter(prefix="/task-runs", tags=["tasks"])


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, TaskNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Task resource not found")
    if isinstance(error, TaskConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, "Task revision conflict")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Task operation failed")


@router.get("")
def list_tasks(auth: AuthContext = Depends(get_auth_context), store: InMemoryTaskStore = Depends(get_task_store)):
    return store.list_tasks(auth.org_id, auth.workspace_id, auth.user)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    request: TaskCreate,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.create_task(auth.org_id, auth.workspace_id, auth.user, request)
    except TaskConflictError as exc:
        raise _translate(exc) from exc


@router.get("/storage-policy")
def get_storage_policy(_auth: AuthContext = Depends(get_auth_context)) -> TaskStoragePolicy:
    return TaskStoragePolicy()


@router.get("/{task_id}")
def get_task(
    task_id: str,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.get_task(auth.org_id, auth.workspace_id, auth.user, task_id)
    except TaskNotFoundError as exc:
        raise _translate(exc) from exc


@router.patch("/{task_id}")
def update_task(
    task_id: str,
    request: TaskPatch,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.update_task(auth.org_id, auth.workspace_id, auth.user, task_id, request)
    except (TaskNotFoundError, TaskConflictError) as exc:
        raise _translate(exc) from exc


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: str,
    delete_history: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        store.delete_task(auth.org_id, auth.workspace_id, auth.user, task_id, delete_history)
    except TaskNotFoundError as exc:
        raise _translate(exc) from exc


@router.get("/{task_id}/runs")
def list_task_runs(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.list_task_runs(auth.org_id, auth.workspace_id, auth.user, task_id, limit, offset)
    except TaskNotFoundError as exc:
        raise _translate(exc) from exc


@router.post("/{task_id}/runs", status_code=status.HTTP_201_CREATED)
def create_task_run(
    task_id: str,
    request: TaskRunCreate,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.create_run(auth.org_id, auth.workspace_id, auth.user, task_id, request)
    except (TaskNotFoundError, TaskConflictError) as exc:
        raise _translate(exc) from exc


@router.patch("/runs/{run_id}")
def update_task_run(
    run_id: str,
    request: TaskRunPatch,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    try:
        return store.update_run(auth.org_id, auth.workspace_id, auth.user, run_id, request)
    except TaskNotFoundError as exc:
        raise _translate(exc) from exc


@runs_router.get("")
def list_runs(
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    return store.list_runs(auth.org_id, auth.workspace_id, auth.user, limit=limit, offset=offset)


@devices_router.post("/heartbeat")
def heartbeat(
    request: DeviceHeartbeat,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    return store.heartbeat(auth.org_id, auth.workspace_id, auth.user, request)


@devices_router.get("")
def list_devices(auth: AuthContext = Depends(get_auth_context), store: InMemoryTaskStore = Depends(get_task_store)):
    return store.list_devices(auth.org_id, auth.workspace_id, auth.user)


class MarkReadRequest(BaseModel):
    ids: list[str] = Field(max_length=500)


@notifications_router.get("")
def list_notifications(
    unread_only: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    return store.list_notifications(auth.org_id, auth.workspace_id, auth.user, unread_only)


@notifications_router.post("/mark-read")
def mark_notifications_read(
    request: MarkReadRequest,
    auth: AuthContext = Depends(get_auth_context),
    store: InMemoryTaskStore = Depends(get_task_store),
):
    return {"updated": store.mark_notifications_read(auth.org_id, auth.workspace_id, auth.user, request.ids)}
