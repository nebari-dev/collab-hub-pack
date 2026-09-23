"""Response models of the Cog catalog read API (issue #85).

:class:`CogCardModel` types the top-level keys the bundle reader emits
(:class:`~.bundle.CogCard`) and nothing else: the card is the Cog's own
declarations, not a hub-invented schema (ADR-0001 D9), so structures the
reader passes through as it found them (``profile``, ``frontmatter``,
``io``, ``provides``, ...) stay open here too. ``extra="allow"`` keeps a
key a newer reader adds from being dropped on the way out.

The rest wrap a card with what only the catalog knows -- where the artifact
lives and when it was seen -- because identity is the digest, and the card
itself does not carry one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .catalog import CogArtifact


class CogCardModel(BaseModel):
    """A Cog's catalog card, exactly as the bundle reader produced it at index time."""

    model_config = ConfigDict(extra="allow")

    # Build-tool keys, in the order the Cog build tooling's `card --json` prints them.
    card: int = 1
    manifest: str | None = Field(default=None, description="Bundle-relative path of the manifest COG.md names.")
    audience_inferred: bool = False
    provides: Any = Field(default_factory=list)
    locality: Any = None
    model: dict[str, Any] | None = None
    id: str | None = Field(default=None, description="The Cog id (`<publisher>/<name>`); a search key, not identity.")
    version: Any = None
    kind: Any = None
    summary: str = ""
    owner: Any = None
    license: Any = None
    io: Any = Field(default=None, description="Declared `accepts` / `produces` io types.")
    entry_points: list[dict[str, Any]] = Field(default_factory=list)
    ops: dict[str, list[str]] = Field(default_factory=lambda: {"usage": [], "lifecycle": []})
    requires: list[dict[str, Any]] = Field(default_factory=list)
    prohibits: Any = Field(default_factory=list)
    input_contract: Any = None
    output_contract: Any = None
    envelope: int | None = None
    fixtures: Any = Field(default_factory=list)

    # Hub-side keys.
    name: str | None = None
    description: str | None = None
    publisher: str | None = None
    manifest_schema: str | None = None
    profile_status: str = "missing"
    profile_schema: str | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    frontmatter_raw: str = ""
    profile: dict[str, Any] | None = None
    profile_raw: str = ""
    body: str = Field(default="", description="The Markdown body of COG.md, after its frontmatter.")
    errors: list[str] = Field(default_factory=list, description="Every problem the reader found.")
    warnings: list[str] = Field(default_factory=list)


class CogVersion(BaseModel):
    """One indexed location of one version: a digest in one repository of one source."""

    digest: str = Field(description="`sha256:<64 hex>`: the artifact's identity.")
    version: str | None = None
    source_id: str
    repository: str
    reference: str = Field(description="The pinned install reference `<host>/<repository>@<digest>`.")
    tags: list[str] = Field(default_factory=list)
    pushed_at: datetime | None = None
    indexed_at: datetime | None = None
    removed_at: datetime | None = Field(
        default=None, description="Set once the artifact is gone from its registry; the row stays readable."
    )

    @classmethod
    def of(cls, row: CogArtifact) -> CogVersion:
        return cls(
            digest=row.digest,
            version=row.version,
            source_id=row.source_id,
            repository=row.repository,
            reference=row.reference,
            tags=list(row.tags),
            pushed_at=row.pushed_at,
            indexed_at=row.indexed_at,
            removed_at=row.removed_at,
        )


class CogEntry(CogVersion):
    """One version's location plus its card."""

    cog_id: str
    card: CogCardModel

    @classmethod
    def of(cls, row: CogArtifact) -> CogEntry:
        return cls(
            **CogVersion.of(row).model_dump(),
            cog_id=row.cog_id or "",
            card=CogCardModel.model_validate(row.card or {}),
        )


class CogListPage(BaseModel):
    """One page of current Cogs, ordered by `cog_id`."""

    items: list[CogEntry]
    limit: int
    offset: int
    next_offset: int | None = Field(description="Pass as `offset` for the next page; null on the last page.")


class CogDetail(CogEntry):
    """A Cog's current version (the newest present one) plus every indexed version, newest first."""

    versions: list[CogVersion] = Field(description="Every indexed location of every version, removed ones included.")


class CogLocation(BaseModel):
    """Another place the same digest was indexed."""

    reference: str
    source_id: str
    repository: str
    pushed_at: datetime | None = None
    removed_at: datetime | None = None

    @classmethod
    def of(cls, row: CogArtifact) -> CogLocation:
        return cls(
            reference=row.reference,
            source_id=row.source_id,
            repository=row.repository,
            pushed_at=row.pushed_at,
            removed_at=row.removed_at,
        )


class CogReference(BaseModel):
    """What a client hands to `nebi import`: the pinned reference of one digest."""

    reference: str = Field(description="`<host>/<repository>@<digest>` of the preferred location.")
    source_id: str
    repository: str
    digest: str
    present: bool = Field(description="False when every location of this digest has been removed from its registry.")
    locations: list[CogLocation] = Field(description="The digest's other indexed locations, preferred first.")


class CatalogV1Repository(BaseModel):
    namespace: str = Field(description="The first path segment of the repository.")
    name: str = Field(description="The rest of the repository path.")
    description: str = ""


class CatalogV1(BaseModel):
    """Transitional: the static `catalog.v1.json` shape, generated from the index."""

    schemaVersion: Literal[1] = 1
    repositories: list[CatalogV1Repository]


class CogErrorBody(BaseModel):
    code: str
    message: str


class CogErrorResponse(BaseModel):
    """The API error envelope."""

    error: CogErrorBody
