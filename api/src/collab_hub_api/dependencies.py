from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from .frames.active_state import ActiveFrameStore
    from .frames.groups import FrameGroupStore
    from .frames.history import FrameHistoryStore
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
