from __future__ import annotations

import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from ..dependencies import (
    get_active_frame_store,
    get_frames_store,
    get_group_store,
    get_history_store,
)
from ..frames.access import can_manage, can_read, can_read_group
from ..frames.active_state import ActiveFrameStore, ActiveStateUnavailableError
from ..frames.auth import AuthContext, get_auth_context
from ..frames.groups import (
    FrameGroup,
    FrameGroupNotFoundError,
    FrameGroupStore,
    FrameGroupStoreUnavailableError,
    project_group,
)
from ..frames.history import (
    FrameHistoryStore,
    HistoryUnavailableError,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from ..frames.models import (
    ID_PATTERN,
    ActiveFramesResponse,
    ActiveFramesUpdate,
    EmailBody,
    Frame,
    FrameCreate,
    FrameMetadata,
    FrameUpdate,
    HistoryEntryResponse,
    HistoryResponse,
    OwnersReplace,
    OwnersResponse,
    ReadersReplace,
    ReadersResponse,
    Suggestion,
    SuggestionCreate,
    SuggestionStatus,
    Visibility,
    normalize_owners,
    normalize_readers,
)
from ..frames.observability import HISTORY_WRITE_FAILURES, audit_event
from ..frames.store import FrameNotFoundError, FrameStore, SuggestionNotFoundError

router = APIRouter(tags=["frames"])

history_logger = logging.getLogger("frames_server.history")

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
StoreDep = Annotated[FrameStore, Depends(get_frames_store)]
ActiveStoreDep = Annotated[ActiveFrameStore, Depends(get_active_frame_store)]
HistoryStoreDep = Annotated[FrameHistoryStore, Depends(get_history_store)]
GroupStoreDep = Annotated[FrameGroupStore, Depends(get_group_store)]


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def require_readable_frame(frame: Frame, auth: AuthContext) -> Frame:
    """Read-check a Frame; 404 (never 403) on read failure.

    No scope pre-gate: ``can_read`` owns scope so a cross-tenant ``public`` read
    is allowed while ``internal``/``private`` stay scoped (Spec 1 §2).
    """

    if not can_read(frame, auth):
        raise FrameNotFoundError(frame.id)
    return frame


def require_manageable_frame(frame: Frame, auth: AuthContext) -> Frame:
    """Read-then-manage check (Spec 1 §2): 404 if unreadable, else 403 if not an owner.

    Evaluating ``can_read`` first means a caller who cannot even see a frame gets
    a 404 (existence is never leaked); one who can read it (a reader, or any user
    on a ``public``/``internal`` frame) but is not an in-tenant owner gets 403.
    ``can_manage`` is tenant-scoped, so ``public`` permits cross-tenant read but
    never cross-tenant management.
    """

    if not can_read(frame, auth):
        raise FrameNotFoundError(frame.id)
    if not can_manage(frame, auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner may manage a Frame",
        )
    return frame


def reconcile_active(frame: Frame, active_store: ActiveFrameStore) -> None:
    """Prune a Frame from the active sets of users who can no longer read it.

    Holder lookup is global (across tenants) because a ``public`` Frame can be
    activated by users in other tenants; each holder is probed with their OWN
    ``(org, workspace)`` so ``can_read``'s tenant check evaluates correctly.
    Idempotent and safe to call unconditionally after any narrowing mutation
    (unpublish, frame update, owners/readers changes). A no-op when active state
    is disabled.
    """

    for org_id, workspace_id, user in active_store.find_active_holders(frame.id):
        probe = AuthContext(user=user, home_org_id=org_id, workspace_id=workspace_id)
        if not can_read(frame, probe):
            active_store.remove_frame_id_for(org_id, workspace_id, user, frame.id)


def _scalar_value(value: object) -> object:
    """Render a scalar field for the history ``detail`` payload."""

    return value.value if isinstance(value, Enum) else value


def scalar_diff(before: Frame, after: Frame, fields: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Return ``{field: {from, to}}`` for scalar fields that changed."""

    diff: dict[str, dict[str, object]] = {}
    for field in fields:
        old = _scalar_value(getattr(before, field))
        new = _scalar_value(getattr(after, field))
        if old != new:
            diff[field] = {"from": old, "to": new}
    return diff


def list_diff(before: list[str], after: list[str]) -> dict[str, list[str]]:
    """Return added/removed identities between two lists (order-insensitive)."""

    before_set, after_set = set(before), set(after)
    return {
        "added": [item for item in after if item not in before_set],
        "removed": [item for item in before if item not in after_set],
    }


def record_frame_history(
    history_store: FrameHistoryStore,
    auth: AuthContext,
    frame: Frame,
    event: str,
    detail: dict | None = None,
) -> None:
    """Record one Frame change event, best-effort relative to the mutation.

    ``actor`` is the Hub user (``auth.user``), never the document ``author``. A
    history-write failure is logged and counted but must never fail or roll back
    the mutation that already succeeded (proposal §6 open item; non-fatal).
    """

    try:
        history_store.record(
            frame.org_id,
            frame.workspace_id,
            "frame",
            frame.id,
            event,
            auth.user,
            detail,
        )
    except Exception:
        HISTORY_WRITE_FAILURES.labels(event=event).inc()
        history_logger.exception(
            "history_write_failed",
            extra={"action": event, "user": auth.user, "frame_id": frame.id},
        )


def record_group_history(
    history_store: FrameHistoryStore,
    auth: AuthContext,
    group: FrameGroup,
    event: str,
    detail: dict | None = None,
) -> None:
    """Record one Frame Group change event into the shared history store.

    Reuses ``frames_server_history`` with ``entity_type='group'``. Best-effort
    relative to the mutation, exactly like ``record_frame_history``: a failed
    write is logged and counted but never fails or rolls back the mutation.
    """

    try:
        history_store.record(
            group.org_id,
            group.workspace_id,
            "group",
            group.id,
            event,
            auth.user,
            detail,
        )
    except Exception:
        HISTORY_WRITE_FAILURES.labels(event=event).inc()
        history_logger.exception(
            "history_write_failed",
            extra={"action": event, "user": auth.user, "group_id": group.id},
        )


def _remove_deleted_member_from_group(
    group: FrameGroup,
    frame_id: str,
    group_store: FrameGroupStore,
    history_store: FrameHistoryStore,
    auth: AuthContext,
) -> None:
    """Drop a **deleted** Frame from one group, cascading to a group delete if it was the last member.

    Both history branches record a ``reason`` so an audit reader can tell this
    membership change was driven by Frame deletion (not a manual removal):
    ``last_member_deleted`` for the cascade, ``member_frame_deleted`` for the
    plain removal.
    """

    if group.frame_ids == [frame_id]:
        group_store.delete_group(group.id)
        record_group_history(
            history_store,
            auth,
            group,
            "deleted",
            {"reason": "last_member_deleted", "frame_id": frame_id},
        )
    else:
        updated = group_store.remove_frame(group.id, frame_id)
        record_group_history(
            history_store,
            auth,
            updated,
            "frame_removed",
            {"reason": "member_frame_deleted", "frame_id": frame_id},
        )


def reconcile_groups_after_frame_delete(
    frame: Frame,
    group_store: FrameGroupStore,
    history_store: FrameHistoryStore,
    auth: AuthContext,
) -> None:
    """Prune a **deleted** Frame from every group it belonged to, in ANY tenant.

    Frame deletion is the ONLY event that mutates group membership. Access
    *narrowing* (unpublish, visibility change, reader/owner changes) is
    deliberately non-destructive — a member that becomes unreadable stays in the
    group and recovers automatically if access is later restored; clients render
    it as inaccessible in the meantime. Deletion is different: the Frame is gone
    for good, so a stale id would linger forever (a missing member counts as
    not-published, silently forcing the group owner-only).

    Lookup is global (``find_groups_containing``), not tenant-scoped: group-add
    accepts readable-not-owned members, including cross-tenant ``public``
    frames, so a deleted Frame's group memberships can live outside its own
    org/workspace. A group whose only member was the deleted Frame is removed
    entirely, preserving the ``>=1``-member invariant. This is system
    reconciliation, so it runs regardless of who owns the group.

    The actor recorded in group history is ``auth.user`` — the Frame owner who
    triggered the deletion, who may belong to a different tenant than the group.
    This is a conscious, audit-correct choice: history records the identity that
    actually caused the change, not a synthetic system actor. A no-op when no
    groups DB is configured — no groups can exist without one, so the frame
    deletion must not 503 because of it.
    """

    try:
        groups = group_store.find_groups_containing(frame.id)
    except FrameGroupStoreUnavailableError:
        return
    for group in groups:
        _remove_deleted_member_from_group(group, frame.id, group_store, history_store, auth)


@router.get("/frames", response_model=list[FrameMetadata])
def list_frames(
    auth: AuthDep,
    store: StoreDep,
    group_store: GroupStoreDep,
    name: str | None = Query(default=None),
    tags: list[str] | None = Query(default=None, alias="tag"),
    owner: str | None = Query(default=None),
    visibility: Visibility | None = Query(default=None),
    published: bool | None = Query(default=None),
    group_id: str | None = Query(default=None),
) -> list[FrameMetadata]:
    # The reciprocal `group_ids` projection on each frame is deferred (groups.py),
    # so the `group_id` filter is resolved from the group side: a single lookup
    # of the group's stored membership rather than a per-frame projection. The
    # group must itself be readable by the caller (Spec 3 §3 — projection
    # includes only groups "that the caller can read"); an unknown, cross-scope,
    # or unreadable group yields no members (empty result).
    group_member_ids: set[str] | None = None
    if group_id is not None:
        try:
            group = project_group(group_store.get_group(group_id), store)
        except (FrameGroupNotFoundError, FrameGroupStoreUnavailableError):
            # No DB ⟹ no groups exist; the filter resolves to no members rather
            # than 503-ing this (frame-list) endpoint over a groups-only concern.
            return []
        if group.org_id != auth.org_id or group.workspace_id != auth.workspace_id:
            return []
        if not can_read_group(group, auth):
            return []
        group_member_ids = set(group.frame_ids)
    items = store.list_frames(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        name=name,
        tags=tags,
        owner=owner,
    )
    results = []
    for item in items:
        if not can_read(item, auth):
            continue
        if visibility is not None and item.visibility != visibility:
            continue
        if published is not None and item.published != published:
            continue
        if group_member_ids is not None and item.id not in group_member_ids:
            continue
        results.append(item)
    return results


@router.get("/frames/{frame_id}", response_model=Frame)
def get_frame(
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> Frame:
    return require_readable_frame(store.get_frame(frame_id), auth)


@router.post("/frames", response_model=Frame, status_code=status.HTTP_201_CREATED)
def create_frame(
    payload: FrameCreate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    history_store: HistoryStoreDep,
) -> Frame:
    owners = normalize_owners([auth.user, *payload.owners])
    frame = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by=auth.user,
        owners=owners,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        tags=payload.tags,
        body=payload.body,
    )
    audit_event("frame_create", request, user=auth.user, frame_id=frame.id)
    record_frame_history(history_store, auth, frame, "created", {"name": frame.name})
    return frame


@router.put("/frames/{frame_id}", response_model=Frame)
def update_frame(
    payload: FrameUpdate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> Frame:
    before = require_manageable_frame(store.get_frame(frame_id), auth)
    updated = store.update_frame(
        frame_id,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        tags=payload.tags,
        body=payload.body,
    )
    reconcile_active(updated, active_store)
    audit_event("frame_update", request, user=auth.user, frame_id=frame_id)
    detail = scalar_diff(before, updated, ("name", "description", "visibility"))
    tags_change = list_diff(before.tags, updated.tags)
    if tags_change["added"] or tags_change["removed"]:
        detail["tags"] = tags_change
    record_frame_history(history_store, auth, updated, "updated", detail)
    if before.visibility != updated.visibility:
        record_frame_history(
            history_store,
            auth,
            updated,
            "visibility_changed",
            {"visibility": {"from": before.visibility.value, "to": updated.visibility.value}},
        )
    return updated


@router.delete("/frames/{frame_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_frame(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    group_store: GroupStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> None:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    store.delete_frame(frame_id)
    active_store.remove_frame_id(frame_id)
    reconcile_groups_after_frame_delete(frame, group_store, history_store, auth)
    audit_event("frame_delete", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, frame, "deleted", {})


@router.get("/frames/{frame_id}/owners", response_model=OwnersResponse)
def get_owners(
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    frame = require_readable_frame(store.get_frame(frame_id), auth)
    return OwnersResponse(owners=frame.owners)


@router.put("/frames/{frame_id}/owners", response_model=OwnersResponse)
def replace_owners(
    payload: OwnersReplace,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    before = require_manageable_frame(store.get_frame(frame_id), auth)
    updated = store.set_owners(frame_id, payload.owners)
    reconcile_active(updated, active_store)
    audit_event("frame_owners_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "owners_changed", list_diff(before.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.post("/frames/{frame_id}/owners", response_model=OwnersResponse)
def add_owner(
    payload: EmailBody,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> OwnersResponse:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    owners = normalize_owners([*frame.owners, payload.email])
    updated = store.set_owners(frame_id, owners)
    audit_event("frame_owners_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "owners_changed", list_diff(frame.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.delete("/frames/{frame_id}/owners/{email}", response_model=OwnersResponse)
def remove_owner(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
    email: str = Path(),
) -> OwnersResponse | JSONResponse:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    remaining = [owner for owner in frame.owners if owner != email]
    if len(remaining) == len(frame.owners):
        raise FrameNotFoundError(frame_id)
    if not remaining:
        return error_response(
            status.HTTP_409_CONFLICT,
            "last_owner",
            "A Frame must always have at least one owner",
        )
    updated = store.set_owners(frame_id, remaining)
    reconcile_active(updated, active_store)
    audit_event("frame_owners_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "owners_changed", list_diff(frame.owners, updated.owners))
    return OwnersResponse(owners=updated.owners)


@router.get("/frames/{frame_id}/readers", response_model=ReadersResponse)
def get_readers(
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> ReadersResponse:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    return ReadersResponse(readers=frame.readers)


@router.put("/frames/{frame_id}/readers", response_model=ReadersResponse)
def replace_readers(
    payload: ReadersReplace,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> ReadersResponse:
    before = require_manageable_frame(store.get_frame(frame_id), auth)
    updated = store.set_readers(frame_id, payload.readers)
    reconcile_active(updated, active_store)
    audit_event("frame_readers_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "readers_changed", list_diff(before.readers, updated.readers))
    return ReadersResponse(readers=updated.readers)


@router.post("/frames/{frame_id}/readers", response_model=ReadersResponse)
def add_reader(
    payload: EmailBody,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> ReadersResponse:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    readers = normalize_readers([*frame.readers, payload.email])
    updated = store.set_readers(frame_id, readers)
    # A non-empty reader write forces visibility=private (invariant), so an
    # internal/public frame flips to private + a reader allowlist. Reconcile to
    # prune any holder (incl. the former whole-tenant/public audience) who can no
    # longer read it. Group membership is intentionally NOT touched — narrowing
    # is non-destructive; a member that becomes unreadable stays and recovers.
    reconcile_active(updated, active_store)
    audit_event("frame_readers_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "readers_changed", list_diff(frame.readers, updated.readers))
    return ReadersResponse(readers=updated.readers)


@router.delete("/frames/{frame_id}/readers/{email}", response_model=ReadersResponse)
def remove_reader(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
    email: str = Path(),
) -> ReadersResponse:
    frame = require_manageable_frame(store.get_frame(frame_id), auth)
    remaining = [reader for reader in frame.readers if reader != email]
    if len(remaining) == len(frame.readers):
        raise FrameNotFoundError(frame_id)
    updated = store.set_readers(frame_id, remaining)
    reconcile_active(updated, active_store)
    audit_event("frame_readers_update", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "readers_changed", list_diff(frame.readers, updated.readers))
    return ReadersResponse(readers=updated.readers)


@router.post("/frames/{frame_id}/publish", response_model=Frame)
def publish_frame(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> Frame:
    require_manageable_frame(store.get_frame(frame_id), auth)
    updated = store.set_published(frame_id, True)
    audit_event("frame_publish", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "published", {})
    return updated


@router.post("/frames/{frame_id}/unpublish", response_model=Frame)
def unpublish_frame(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> Frame:
    require_manageable_frame(store.get_frame(frame_id), auth)
    updated = store.set_published(frame_id, False)
    reconcile_active(updated, active_store)
    audit_event("frame_unpublish", request, user=auth.user, frame_id=frame_id)
    record_frame_history(history_store, auth, updated, "unpublished", {})
    return updated


@router.post(
    "/frames/{frame_id}/suggestions",
    response_model=Suggestion,
    status_code=status.HTTP_201_CREATED,
)
def create_suggestion(
    payload: SuggestionCreate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
) -> Suggestion:
    require_readable_frame(store.get_frame(frame_id), auth)
    suggestion = store.create_suggestion(frame_id=frame_id, submitted_by=auth.user, body=payload.body)
    audit_event(
        "suggestion_create",
        request,
        user=auth.user,
        frame_id=frame_id,
        suggestion_id=suggestion.id,
    )
    return suggestion


@router.get("/frames/{frame_id}/suggestions", response_model=list[Suggestion])
def list_suggestions(
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
    status_filter: SuggestionStatus | None = Query(default=None, alias="status"),
) -> list[Suggestion]:
    require_readable_frame(store.get_frame(frame_id), auth)
    return store.list_suggestions(frame_id, status=status_filter)


@router.post("/frames/{frame_id}/suggestions/{suggestion_id}/close", response_model=Suggestion)
def close_suggestion(
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
    suggestion_id: str = Path(pattern=ID_PATTERN),
) -> Suggestion:
    frame = require_readable_frame(store.get_frame(frame_id), auth)
    suggestion = next((item for item in frame.suggestions if item.id == suggestion_id), None)
    if suggestion is None:
        raise SuggestionNotFoundError(suggestion_id)
    if auth.user not in {*frame.owners, suggestion.submitted_by}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the Frame owner or Suggestion submitter may close a Suggestion",
        )
    suggestion = store.close_suggestion(frame_id, suggestion_id)
    audit_event(
        "suggestion_close",
        request,
        user=auth.user,
        frame_id=frame_id,
        suggestion_id=suggestion_id,
    )
    return suggestion


@router.get("/frames/{frame_id}/history", response_model=HistoryResponse)
def get_frame_history(
    auth: AuthDep,
    store: StoreDep,
    history_store: HistoryStoreDep,
    frame_id: str = Path(pattern=ID_PATTERN),
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
) -> HistoryResponse | JSONResponse:
    # History rows survive frame deletion in the store, but the endpoint
    # authorizes against the live frame (require_readable_frame / can_read), so a
    # deleted frame's history is intentionally not API-readable — the persisted
    # rows are durable for future admin/audit tooling only.
    frame = require_readable_frame(store.get_frame(frame_id), auth)
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
    # Query history under the FRAME's stored tenant, not the caller's: a
    # cross-tenant reader of a `public` frame can read it (can_read passed) and
    # its history rows live under the frame's own (org, workspace).
    # Over-fetch one row to detect a further page without an empty trailing
    # request: `next` is null exactly when the history is exhausted.
    rows = history_store.query(
        frame.org_id,
        frame.workspace_id,
        "frame",
        frame_id,
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


@router.get("/active-frames", response_model=ActiveFramesResponse)
def get_active_frames(auth: AuthDep, active_store: ActiveStoreDep) -> ActiveFramesResponse:
    return ActiveFramesResponse(
        user=auth.user,
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        frame_ids=active_store.get_active_frame_ids(auth.org_id, auth.workspace_id, auth.user),
    )


@router.put("/active-frames", response_model=ActiveFramesResponse)
def set_active_frames(
    payload: ActiveFramesUpdate,
    request: Request,
    auth: AuthDep,
    store: StoreDep,
    active_store: ActiveStoreDep,
) -> ActiveFramesResponse:
    for frame_id in payload.frame_ids:
        require_readable_frame(store.get_frame(frame_id), auth)
    frame_ids = active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, payload.frame_ids)
    audit_event("active_frames_update", request, user=auth.user)
    return ActiveFramesResponse(
        user=auth.user,
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        frame_ids=frame_ids,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FrameNotFoundError)
    async def frame_not_found_handler(_request: Request, _exc: FrameNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "frame_not_found", "Frame not found")

    @app.exception_handler(SuggestionNotFoundError)
    async def suggestion_not_found_handler(_request: Request, _exc: SuggestionNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "suggestion_not_found", "Suggestion not found")

    @app.exception_handler(ActiveStateUnavailableError)
    async def active_state_unavailable_handler(_request: Request, exc: ActiveStateUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "active_state_unavailable", str(exc))

    @app.exception_handler(HistoryUnavailableError)
    async def history_unavailable_handler(_request: Request, exc: HistoryUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "history_unavailable", str(exc))
