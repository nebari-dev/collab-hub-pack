"""Single source of truth for Frame access decisions.

Imported by both the REST router and the MCP server so model-context injection
and the HTTP API share identical access logic. `can_read` accepts
``FrameMetadata`` (no body required), so it works for list results as well as
full Frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .auth import AuthContext
from .models import FrameMetadata, Visibility

if TYPE_CHECKING:
    from .groups import FrameGroup


def can_read(frame: FrameMetadata, auth: AuthContext) -> bool:
    """Return whether the caller may read a Frame.

    ``published`` is the master gate: until a Frame is published, only its
    owners can access it, regardless of visibility or reader list. Once
    published, the three visibility tiers apply (Spec 1 §2):

    - ``public`` — any authenticated user, in ANY tenant (cross-tenant).
    - ``internal`` — the whole of the frame's own tenant (readers do not apply).
    - ``private`` — owners, plus any user on the ``readers`` list (the reader
      list is an ACL-lite grant that *expands* a private frame).

    The reader/visibility invariant (non-empty ``readers`` ⟹ ``visibility ==
    private``) is enforced in the store, so readers are only ever consulted on
    the private branch.

    This function owns the scope check, so callers must NOT scope-pre-gate
    before it (that would block legitimate cross-tenant ``public`` reads).
    """

    if auth.user in frame.owners:
        return True
    if not frame.published:
        return False
    if frame.visibility == Visibility.public:
        return True
    if frame.visibility == Visibility.internal:
        # Whole tenant; readers do NOT apply to internal (invariant: non-empty
        # readers ⟹ visibility=private).
        return (frame.org_id, frame.workspace_id) == (auth.org_id, auth.workspace_id)
    # private: the reader list expands access to listed users.
    if auth.user in frame.readers:
        return True
    return False


def can_manage(frame: FrameMetadata, auth: AuthContext) -> bool:
    """Return whether the caller may modify, delete, publish, or manage a Frame.

    Management is **tenant-scoped**: an owner may manage only from the frame's own
    ``(org_id, workspace_id)``. ``public`` grants cross-tenant *read by id*, never
    cross-tenant mutation (Apollo A3 Proposal §3.1). So unlike ``can_read`` — which
    deliberately drops the scope check — manage keeps it.
    """

    return auth.user in frame.owners and (frame.org_id, frame.workspace_id) == (
        auth.org_id,
        auth.workspace_id,
    )


def can_read_group(group: FrameGroup, auth: AuthContext) -> bool:
    """Return whether the caller may read a Frame Group.

    Reads the group's **derived** fields, so the group must be projected first
    (see ``groups.project_group``). A group is visible to non-owners only once
    **every** member Frame is published (PRD §2.3 readiness gate); then the
    group's ``effective_visibility`` — the least-broad of its own visibility and
    every member's — decides the audience: ``public`` reads cross-tenant,
    ``internal`` reads same-tenant, ``private`` stays owner-only.
    """

    if auth.user in group.owners:
        return True
    if not group.all_published:
        return False
    if group.effective_visibility == Visibility.public:
        return True
    if group.effective_visibility == Visibility.internal:
        return group.org_id == auth.org_id and group.workspace_id == auth.workspace_id
    return False


def can_manage_group(group: FrameGroup, auth: AuthContext) -> bool:
    """Return whether the caller may modify, delete, or manage a Frame Group.

    Tenant-scoped, mirroring ``can_manage``: a ``public`` group is readable
    cross-tenant but only manageable from its own ``(org_id, workspace_id)``.
    """

    return auth.user in group.owners and (group.org_id, group.workspace_id) == (
        auth.org_id,
        auth.workspace_id,
    )
