from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from ..dependencies import get_frames_store, get_group_store, get_history_store
from ..frames.access import can_manage_group, can_read_group
from ..frames.auth import AuthContext, get_auth_context
from ..frames.groups import (
    FrameGroup,
    FrameGroupCreate,
    FrameGroupNotFoundError,
    FrameGroupStore,
    FrameGroupStoreUnavailableError,
    FrameGroupUpdate,
    FrameIdBody,
    project_group,
)
from ..frames.history import (
    FrameHistoryStore,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from ..frames.models import (
    ID_PATTERN,
    EmailBody,
    HistoryEntryResponse,
    HistoryResponse,
    OwnersReplace,
    OwnersResponse,
    Visibility,
    normalize_owners,
)
from ..frames.observability import audit_event
from ..frames.store import FrameNotFoundError, FrameStore
from .frames import error_response, list_diff, record_group_history, require_readable_frame

router = APIRouter(tags=["frame-groups"])

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
StoreDep = Annotated[FrameStore, Depends(get_frames_store)]
GroupStoreDep = Annotated[FrameGroupStore, Depends(get_group_store)]
HistoryStoreDep = Annotated[FrameHistoryStore, Depends(get_history_store)]


def require_readable_group(group: FrameGroup, store: FrameStore, auth: AuthContext) -> FrameGroup:
    """Read-check a group; 404 (never 403) on read failure.

    No scope pre-gate: ``can_read_group`` owns scope so a cross-tenant ``public``
    group is readable while ``internal``/``private`` stay scoped (Spec 3 §4,
    mirroring Spec 1 §2/§4.1). Returns the group with ``all_published`` populated
    for the response.
    """

    projected = project_group(group, store)
    if not can_read_group(projected, auth):
        raise FrameGroupNotFoundError(group.id)
    return projected


def require_manageable_group(group: FrameGroup, store: FrameStore, auth: AuthContext) -> FrameGroup:
    """Read-then-manage check (Spec 3 §4): 404 if unreadable, else 403 if not an owner.

    Mirrors Spec 1's frame ordering: a group the caller can't see returns 404
    (never a 403 that leaks its existence); a visible group the caller does not
    own (in-tenant) returns 403. ``can_manage_group`` is tenant-scoped, so
    ``public`` permits cross-tenant read but never cross-tenant management.
    Returns the group with ``all_published`` populated.
    """

    projected = require_readable_group(group, store, auth)
    if not can_manage_group(projected, auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner may manage a Frame Group",
        )
    return projected


@router.get("/frame-groups", response_model=list[FrameGroup])
def list_frame_groups(
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    name: str | None = Query(default=None),
    visibility: Visibility | None = Query(default=None),
    published: bool | None = Query(default=None),
) -> list[FrameGroup]:
    results = []
    for group in group_store.list_groups(auth.org_id, auth.workspace_id):
        projected = project_group(group, store)
        if not can_read_group(projected, auth):
            continue
        if visibility is not None and projected.visibility != visibility:
            continue
        if published is not None and projected.all_published != published:
            continue
        if name and name.casefold() not in projected.name.casefold():
            continue
        results.append(projected)
    return results


@router.post("/frame-groups", response_model=FrameGroup, status_code=status.HTTP_201_CREATED)
def create_frame_group(
    payload: FrameGroupCreate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
) -> FrameGroup:
    # Every member frame must be readable by the caller (Spec 1 access): a
    # frame they can't even see 404s; ownership is not required to bundle it.
    for frame_id in payload.frame_ids:
        require_readable_frame(store.get_frame(frame_id), auth)
    group = group_store.create_group(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by=auth.user,
        owners=[auth.user],
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        frame_ids=payload.frame_ids,
    )
    audit_event("group_create", request, user=auth.user)
    record_group_history(history_store, auth, group, "created", {"name": group.name})
    return project_group(group, store)


@router.get("/frame-groups/{group_id}", response_model=FrameGroup)
def get_frame_group(
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> FrameGroup:
    return require_readable_group(group_store.get_group(group_id), store, auth)


@router.put("/frame-groups/{group_id}", response_model=FrameGroup)
def update_frame_group(
    payload: FrameGroupUpdate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> FrameGroup:
    before = require_manageable_group(group_store.get_group(group_id), store, auth)
    updated = group_store.update_group(
        group_id,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
    )
    audit_event("group_update", request, user=auth.user)
    detail: dict[str, dict[str, object]] = {}
    for field in ("name", "description"):
        old, new = getattr(before, field), getattr(updated, field)
        if old != new:
            detail[field] = {"from": old, "to": new}
    if before.visibility != updated.visibility:
        detail["visibility"] = {"from": before.visibility.value, "to": updated.visibility.value}
    record_group_history(history_store, auth, updated, "updated", detail)
    return project_group(updated, store)


@router.delete("/frame-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_frame_group(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> None:
    group = require_manageable_group(group_store.get_group(group_id), store, auth)
    # Deleting a group never deletes its member frames — membership is the only
    # thing removed.
    group_store.delete_group(group_id)
    audit_event("group_delete", request, user=auth.user)
    record_group_history(history_store, auth, group, "deleted", {})


@router.post("/frame-groups/{group_id}/frames", response_model=FrameGroup)
def add_group_frame(
    payload: FrameIdBody,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> FrameGroup:
    require_manageable_group(group_store.get_group(group_id), store, auth)
    # The added frame must be readable by the caller (Spec 1 access); ownership
    # of the frame is not required, only ownership of the group.
    require_readable_frame(store.get_frame(payload.frame_id), auth)
    updated = group_store.add_frame(group_id, payload.frame_id)
    audit_event("group_frame_add", request, user=auth.user, frame_id=payload.frame_id)
    record_group_history(
        history_store,
        auth,
        updated,
        "frame_added",
        {"frame_id": payload.frame_id},
    )
    return project_group(updated, store)


@router.delete("/frame-groups/{group_id}/frames/{frame_id}", response_model=FrameGroup)
def remove_group_frame(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
    frame_id: str = Path(pattern=ID_PATTERN),
) -> FrameGroup | JSONResponse:
    group = require_manageable_group(group_store.get_group(group_id), store, auth)
    if frame_id not in group.frame_ids:
        raise FrameNotFoundError(frame_id)
    if len(group.frame_ids) == 1:
        return error_response(
            status.HTTP_409_CONFLICT,
            "last_frame",
            "A Frame Group must always contain at least one frame",
        )
    updated = group_store.remove_frame(group_id, frame_id)
    audit_event("group_frame_remove", request, user=auth.user, frame_id=frame_id)
    record_group_history(history_store, auth, updated, "frame_removed", {"frame_id": frame_id})
    return project_group(updated, store)


@router.get("/frame-groups/{group_id}/owners", response_model=OwnersResponse)
def get_group_owners(
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    group = require_readable_group(group_store.get_group(group_id), store, auth)
    return OwnersResponse(owners=group.owners)


@router.put("/frame-groups/{group_id}/owners", response_model=OwnersResponse)
def replace_group_owners(
    payload: OwnersReplace,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    before = require_manageable_group(group_store.get_group(group_id), store, auth)
    updated = group_store.set_owners(group_id, payload.owners)
    audit_event("group_owners_update", request, user=auth.user)
    record_group_history(history_store, auth, updated, "owners_changed", list_diff(before.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.post("/frame-groups/{group_id}/owners", response_model=OwnersResponse)
def add_group_owner(
    payload: EmailBody,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    group = require_manageable_group(group_store.get_group(group_id), store, auth)
    owners = normalize_owners([*group.owners, payload.email])
    updated = group_store.set_owners(group_id, owners)
    audit_event("group_owners_update", request, user=auth.user)
    record_group_history(history_store, auth, updated, "owners_changed", list_diff(group.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.delete("/frame-groups/{group_id}/owners/{email}", response_model=OwnersResponse)
def remove_group_owner(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
    email: str = Path(),
) -> OwnersResponse | JSONResponse:
    group = require_manageable_group(group_store.get_group(group_id), store, auth)
    remaining = [owner for owner in group.owners if owner != email]
    if len(remaining) == len(group.owners):
        raise FrameGroupNotFoundError(group_id)
    if not remaining:
        return error_response(
            status.HTTP_409_CONFLICT,
            "last_owner",
            "A Frame Group must always have at least one owner",
        )
    updated = group_store.set_owners(group_id, remaining)
    audit_event("group_owners_update", request, user=auth.user)
    record_group_history(history_store, auth, updated, "owners_changed", list_diff(group.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.get("/frame-groups/{group_id}/history", response_model=HistoryResponse)
def get_group_history(
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    history_store: HistoryStoreDep,
    group_id: str = Path(pattern=ID_PATTERN),
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
) -> HistoryResponse | JSONResponse:
    # Authorize against the live group (same contract as frame history): a
    # deleted group's persisted rows are durable but not API-readable.
    group = require_readable_group(group_store.get_group(group_id), store, auth)
    cursor = None
    if before is not None:
        try:
            cursor = decode_cursor(before)
        except InvalidCursorError:
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "invalid_cursor",
                "Malformed pagination cursor",
            )
    # Query under the GROUP's stored tenant, not the caller's, so a cross-tenant
    # reader of a `public` group sees its history (rows live under the group's
    # own (org, workspace)).
    # Over-fetch one row to detect a further page without an empty trailing
    # request: `next` is null exactly when the history is exhausted.
    rows = history_store.query(
        group.org_id,
        group.workspace_id,
        "group",
        group_id,
        limit + 1,
        before=cursor,
    )
    entries = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = entries[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return HistoryResponse(
        entries=[
            HistoryEntryResponse(
                id=entry.id,
                event=entry.event,
                actor=entry.actor,
                detail=entry.detail,
                created_at=entry.created_at,
            )
            for entry in entries
        ],
        next=next_cursor,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FrameGroupNotFoundError)
    async def frame_group_not_found_handler(_request: Request, _exc: FrameGroupNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "group_not_found", "Frame Group not found")

    @app.exception_handler(FrameGroupStoreUnavailableError)
    async def frame_groups_unavailable_handler(_request: Request, exc: FrameGroupStoreUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "groups_unavailable", str(exc))
