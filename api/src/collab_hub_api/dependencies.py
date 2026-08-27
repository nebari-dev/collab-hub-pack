from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import Request

# Imported at runtime, not under TYPE_CHECKING: the disabled granter is the
# default value a provider returns, not only a type annotation.
from .frames.account_provisioning import DisabledServiceAccessGranter

if TYPE_CHECKING:
    from .frames.account_provisioning import ServiceAccessGranter
    from .frames.active_state import ActiveFrameStore
    from .frames.groups import FrameGroupStore
    from .frames.history import FrameHistoryStore
    from .frames.invitation_email import InvitationEmailDelivery
    from .frames.invitations import InvitationService
    from .frames.store import FrameStore
    from .frames.usage import UsageStore
    from .tasks.store import InMemoryTaskStore, PostgresTaskStore
    from .user_directory import UserDirectoryClient


def get_frames_store(request: Request) -> FrameStore:
    return request.app.state.frames_store


def get_active_frame_store(request: Request) -> ActiveFrameStore:
    return request.app.state.active_frame_store


def get_history_store(request: Request) -> FrameHistoryStore:
    return request.app.state.history_store


def get_group_store(request: Request) -> FrameGroupStore:
    return request.app.state.group_store


def get_user_directory_client(request: Request) -> UserDirectoryClient:
    return request.app.state.user_directory_client


def get_usage_store(request: Request) -> UsageStore:
    return request.app.state.usage_store


def get_task_store(request: Request) -> InMemoryTaskStore | PostgresTaskStore:
    return request.app.state.task_store


def get_invitation_service(request: Request) -> InvitationService:
    return request.app.state.invitation_service


def get_invitation_email_delivery(request: Request) -> InvitationEmailDelivery:
    return request.app.state.invitation_email_delivery


def get_service_access_granter(request: Request) -> ServiceAccessGranter:
    """The membership-writing seam (#180).

    ``getattr`` with a disabled default rather than a bare attribute read: an
    app assembled some other way -- a test building only what it needs, or a
    deployment predating this -- gets the refusing implementation rather than
    an ``AttributeError`` from inside an acceptance. Refusing to grant is a
    correct state for a deployment; failing an acceptance is not.
    """

    return getattr(request.app.state, "service_access_granter", DisabledServiceAccessGranter())


def get_granted_service_groups(request: Request) -> Sequence[str]:
    """Which groups an acceptance grants, from configuration (#180).

    Empty by default, and empty means grant nothing. The behaviour this
    replaced granted at account creation and therefore reached anyone who
    self-registered (an internal issue); nothing should
    grant service access because a deployment forgot to say otherwise.
    """

    return getattr(request.app.state, "granted_service_groups", ())
