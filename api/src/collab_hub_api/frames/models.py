"""Pydantic models and validation limits for the Frames API."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FRAME_BODY_MAX_LENGTH = 2 * 1024 * 1024
SUGGESTION_BODY_MAX_LENGTH = 64 * 1024
MAX_TAGS = 20
MAX_ACTIVE_FRAMES = 50
MAX_OWNERS = 50
MAX_READERS = 200
DESCRIPTION_MAX_LENGTH = 2048
FRAME_METADATA_SCHEMA_VERSION = 1
FRAME_NAME_MAX_LENGTH = 120
TAG_MAX_LENGTH = 64
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ID_PATTERN = r"^[a-f0-9]{32}$"


class Visibility(str, Enum):
    """Audience for a published Frame.

    - ``private``: owners, plus anyone on the ``readers`` list (readers are an
      ACL-lite grant that *expands* a private frame).
    - ``internal``: the whole of the frame's tenant (readers do not apply).
    - ``public``: any authenticated user in ANY org/workspace (multi-tenant).

    Invariant: a non-empty ``readers`` list implies ``visibility == private``
    (enforced on write and repaired on read in the store).
    """

    private = "private"
    internal = "internal"
    public = "public"


def validate_frame_id(frame_id: str) -> str:
    """Return a valid Frame id or raise ValueError."""

    if not re.fullmatch(ID_PATTERN, frame_id):
        raise ValueError("frame_id must be a valid Frame id")
    return frame_id


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize, deduplicate, and validate tag values for persistence."""

    normalized = []
    seen = set()
    for tag in tags:
        value = tag.strip().lower()
        if not value:
            raise ValueError("tags must not be empty")
        if len(value) > TAG_MAX_LENGTH:
            raise ValueError(f"tags must be {TAG_MAX_LENGTH} characters or fewer")
        if not TAG_PATTERN.fullmatch(value):
            raise ValueError(
                "tags must start with a lowercase letter or number and contain "
                "only lowercase letters, numbers, dots, underscores, or hyphens"
            )
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if len(normalized) > MAX_TAGS:
        raise ValueError(f"frames may have at most {MAX_TAGS} tags")
    return normalized


def normalize_name(name: str) -> str:
    """Trim and validate a human-readable Frame name."""

    value = name.strip()
    if not value:
        raise ValueError("name must not be empty")
    return value


def normalize_owners(owners: list[str], *, require_non_empty: bool = True) -> list[str]:
    """Trim, deduplicate, and cap Frame owner identities (order preserved)."""

    normalized = []
    seen = set()
    for owner in owners:
        value = owner.strip()
        if not value:
            raise ValueError("owners must not be empty")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if require_non_empty and not normalized:
        raise ValueError("frames must have at least one owner")
    if len(normalized) > MAX_OWNERS:
        raise ValueError(f"frames may have at most {MAX_OWNERS} owners")
    return normalized


def normalize_readers(readers: list[str]) -> list[str]:
    """Trim, deduplicate, and cap read-only grantee identities."""

    normalized = []
    seen = set()
    for reader in readers:
        value = reader.strip()
        if not value:
            raise ValueError("readers must not be empty")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if len(normalized) > MAX_READERS:
        raise ValueError(f"frames may have at most {MAX_READERS} readers")
    return normalized


class SuggestionStatus(str, Enum):
    """Lifecycle states supported by prototype Suggestions."""

    open = "open"
    closed = "closed"


class Suggestion(BaseModel):
    """User-submitted Markdown change proposal for a Frame."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    frame_id: str = Field(pattern=ID_PATTERN)
    status: SuggestionStatus = SuggestionStatus.open
    submitted_by: str
    body: str = Field(min_length=1, max_length=SUGGESTION_BODY_MAX_LENGTH)


class FrameMetadata(BaseModel):
    """Frame metadata returned by list endpoints and MCP metadata tools."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = FRAME_METADATA_SCHEMA_VERSION
    id: str = Field(pattern=ID_PATTERN)
    org_id: str
    workspace_id: str
    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility = Visibility.private
    created_by: str
    owners: list[str] = Field(default_factory=list)
    published: bool = False
    readers: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    token_estimate: int = Field(ge=0)
    suggestions: list[Suggestion] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        return normalize_tags(tags)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)

    @field_validator("owners")
    @classmethod
    def validate_owners(cls, owners: list[str]) -> list[str]:
        return normalize_owners(owners)

    @field_validator("readers")
    @classmethod
    def validate_readers(cls, readers: list[str]) -> list[str]:
        return normalize_readers(readers)


class Frame(FrameMetadata):
    """Complete Frame record including the Markdown body."""

    body: str = Field(min_length=1, max_length=FRAME_BODY_MAX_LENGTH)


class FrameCreate(BaseModel):
    """Request body for creating a Frame.

    `owners`, `readers`, and `published` are not settable here — dedicated
    endpoints own them. `owners` may seed co-owners; the caller is always
    force-added by the router.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility = Visibility.private
    owners: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    body: str = Field(min_length=1, max_length=FRAME_BODY_MAX_LENGTH)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        return normalize_tags(tags)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)

    @field_validator("owners")
    @classmethod
    def validate_owners(cls, owners: list[str]) -> list[str]:
        return normalize_owners(owners, require_non_empty=False)


class FrameUpdate(BaseModel):
    """Request body for replacing mutable Frame fields."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility = Visibility.private
    tags: list[str] = Field(default_factory=list)
    body: str = Field(min_length=1, max_length=FRAME_BODY_MAX_LENGTH)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        return normalize_tags(tags)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)


class OwnersReplace(BaseModel):
    """Request body for replacing the full owner list (>=1)."""

    model_config = ConfigDict(extra="forbid")

    owners: list[str] = Field(default_factory=list)

    @field_validator("owners")
    @classmethod
    def validate_owners(cls, owners: list[str]) -> list[str]:
        return normalize_owners(owners)


class ReadersReplace(BaseModel):
    """Request body for replacing the full reader list."""

    model_config = ConfigDict(extra="forbid")

    readers: list[str] = Field(default_factory=list)

    @field_validator("readers")
    @classmethod
    def validate_readers(cls, readers: list[str]) -> list[str]:
        return normalize_readers(readers)


class EmailBody(BaseModel):
    """Request body for adding a single owner or reader by identity."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def validate_email(cls, email: str) -> str:
        value = email.strip()
        if not value:
            raise ValueError("email must not be empty")
        return value


class OwnersResponse(BaseModel):
    """Response shape for owner-list endpoints."""

    model_config = ConfigDict(extra="forbid")

    owners: list[str] = Field(default_factory=list)


class ReadersResponse(BaseModel):
    """Response shape for reader-list endpoints."""

    model_config = ConfigDict(extra="forbid")

    readers: list[str] = Field(default_factory=list)


class SuggestionCreate(BaseModel):
    """Request body for submitting a Suggestion."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=SUGGESTION_BODY_MAX_LENGTH)


class ActiveFramesUpdate(BaseModel):
    """Request body for replacing a user's active Frame selection."""

    model_config = ConfigDict(extra="forbid")

    frame_ids: list[str] = Field(default_factory=list, max_length=MAX_ACTIVE_FRAMES)

    @field_validator("frame_ids")
    @classmethod
    def validate_frame_ids(cls, frame_ids: list[str]) -> list[str]:
        seen = set()
        normalized = []
        for frame_id in frame_ids:
            validate_frame_id(frame_id)
            if frame_id not in seen:
                normalized.append(frame_id)
                seen.add(frame_id)
        return normalized


class ActiveFramesResponse(BaseModel):
    """Response shape for per-user active Frame selection.

    ``user`` identifies whose active selection this is. (Spec 1 removed the
    singular ``owner`` term everywhere; active state is per-user, not owned.)
    """

    model_config = ConfigDict(extra="forbid")

    user: str
    org_id: str
    workspace_id: str
    frame_ids: list[str] = Field(default_factory=list)


class HistoryEntryResponse(BaseModel):
    """One change-history event as returned by the history endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str
    event: str
    actor: str
    detail: dict | None = None
    created_at: datetime


class HistoryResponse(BaseModel):
    """Paginated, newest-first change-history response."""

    model_config = ConfigDict(extra="forbid")

    entries: list[HistoryEntryResponse] = Field(default_factory=list)
    next: str | None = None


class HealthResponse(BaseModel):
    """Readiness/liveness response shape."""

    status: Literal["ok"]


def frame_metadata(frame: Frame) -> FrameMetadata:
    """Project a full Frame to metadata without the Markdown body."""

    return FrameMetadata(**frame.model_dump(exclude={"body"}))
