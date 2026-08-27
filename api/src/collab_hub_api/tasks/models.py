from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


MAX_LOG_BYTES_PER_RUN = 25 * 1024 * 1024
MAX_ARTIFACT_BYTES_PER_RUN = 500 * 1024 * 1024
MAX_TOTAL_TASK_STORAGE_BYTES = 5 * 1024 * 1024 * 1024
MAX_RETAINED_RUNS_PER_TASK = 100


class TaskStoragePolicy(BaseModel):
    max_log_bytes_per_run: int = MAX_LOG_BYTES_PER_RUN
    max_artifact_bytes_per_run: int = MAX_ARTIFACT_BYTES_PER_RUN
    max_total_task_storage_bytes: int = MAX_TOTAL_TASK_STORAGE_BYTES
    max_retained_runs_per_task: int = MAX_RETAINED_RUNS_PER_TASK


class TaskCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=200_000)
    execution_device_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    schedule: dict[str, Any]
    time_zone: str = Field(min_length=1, max_length=100)
    missed_run_policy: Literal["skip", "catch_up_one"] = "skip"
    catch_up_window_seconds: int = Field(default=3600, ge=60, le=604_800)
    concurrency_policy: Literal["skip_if_running"] = "skip_if_running"
    requirements: dict[str, Any] = Field(default_factory=dict)
    connector_requirements: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("title", "prompt", "execution_device_id", "agent_id", "time_zone")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskPatch(BaseModel):
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=200_000)
    execution_device_id: str | None = Field(default=None, min_length=1, max_length=200)
    agent_id: str | None = Field(default=None, min_length=1, max_length=200)
    schedule: dict[str, Any] | None = None
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    missed_run_policy: Literal["skip", "catch_up_one"] | None = None
    catch_up_window_seconds: int | None = Field(default=None, ge=60, le=604_800)
    concurrency_policy: Literal["skip_if_running"] | None = None
    requirements: dict[str, Any] | None = None
    connector_requirements: list[dict[str, Any]] | None = None
    enabled: bool | None = None
    paused_reason: str | None = None
    next_run_at: datetime | None = None

    @field_validator("title", "prompt", "execution_device_id", "agent_id", "time_zone")
    @classmethod
    def strip_required(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskRecord(TaskCreate):
    id: str
    org_id: str
    workspace_id: str
    owner_id: str
    revision: int = 1
    schedule_revision: int = 1
    local_only: bool = True
    paused_reason: str | None = None
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskRunCreate(BaseModel):
    id: str | None = None
    created_at: datetime | None = None
    task_revision: int = Field(ge=1)
    schedule_revision: int = Field(ge=1)
    occurrence_key: str = Field(min_length=1, max_length=500)
    trigger: Literal["manual", "scheduled", "retry", "catch_up"]
    scheduled_at: datetime | None = None
    execution_device_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    task_snapshot: dict[str, Any]
    status: str = "scheduled"
    reason: str | None = None


class TaskRunPatch(BaseModel):
    status: str
    reason: str | None = None
    harness_environment_id: str | None = None
    harness_run_id: str | None = None
    provider_metadata: dict[str, Any] | None = None
    result_metadata: dict[str, Any] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    logs_expires_at: datetime | None = None
    artifacts_expires_at: datetime | None = None


class TaskRunRecord(TaskRunCreate):
    id: str
    task_id: str
    org_id: str
    workspace_id: str
    owner_id: str
    harness_environment_id: str | None = None
    harness_run_id: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    result_metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    logs_expires_at: datetime
    artifacts_expires_at: datetime


class DeviceHeartbeat(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)
    capabilities: list[str] = Field(default_factory=list)
    app_version: str = Field(default="", max_length=100)


class DeviceRecord(DeviceHeartbeat):
    org_id: str
    workspace_id: str
    user_id: str
    last_seen_at: datetime
    expires_at: datetime


class NotificationRecord(BaseModel):
    id: str
    org_id: str
    workspace_id: str
    owner_id: str
    task_id: str
    run_id: str | None = None
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime
