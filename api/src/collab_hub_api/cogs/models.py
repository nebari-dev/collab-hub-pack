"""Response models of the Cog catalog read API (issue #85).

The card is served **as stored**: an open JSON object, never re-validated.
The store keeps the bundle reader's output verbatim (ADR-0001 D9: the card
is the Cog's own declarations, not a hub-invented schema), and the reader
passes publisher values through as it found them -- a profile may declare
``id: 42``, and the card says ``42``. A typed response model would turn one
such card into a failed response for every page it appears on, so the types
live in the OpenAPI description (:data:`CARD_SCHEMA`) and nowhere else.

The rest wrap a card with what only the catalog knows -- where the artifact
lives and when it was seen -- because identity is the digest, and the card
itself does not carry one.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, WithJsonSchema

from .bundle import CogCard
from .catalog import CogArtifact

_CARD_KEY_NOTES = {
    "card": "Card format version (1).",
    "manifest": "Bundle-relative path of the manifest COG.md names.",
    "id": "The Cog id, usually `<publisher>/<name>`: a search key, not identity.",
    "version": "The declared version.",
    "kind": "`complete`, `model`, `harness`, `context`, `prog`, ...",
    "provides": "What the Cog provides (usually a list of strings).",
    "requires": "Required capabilities (usually a list of `{capability, locality, satisfiers}`).",
    "io": "Declared io types (usually `{accepts: [...], produces: [...]}`).",
    "entry_points": "Declared entry points (usually a list of objects).",
    "ops": "Entry points by audience (usually `{usage: [...], lifecycle: [...]}`).",
    "name": "The frontmatter name.",
    "description": "The frontmatter description.",
    "publisher": "The frontmatter publisher.",
    "frontmatter": "The COG.md frontmatter, parsed.",
    "frontmatter_raw": "The COG.md frontmatter, verbatim.",
    "profile": "The whole profile the manifest declares, as structured data.",
    "profile_raw": "The profile file, verbatim.",
    "body": "The Markdown body of COG.md, after its frontmatter.",
    "errors": "Every problem the reader found (usually a list of strings).",
    "warnings": "Non-fatal findings (usually a list of strings).",
}

CARD_SCHEMA: dict[str, Any] = {
    "title": "CogCard",
    "type": "object",
    "description": (
        "A Cog's catalog card: the bundle reader's output as captured at index time, served verbatim. "
        "The keys below are the ones the reader emits, in its order; their values are the Cog's own "
        "declarations and are not validated here, so a client must tolerate unexpected shapes. "
        "Keys a newer reader adds appear too."
    ),
    "properties": {field.name: {"description": _CARD_KEY_NOTES.get(field.name, "")} for field in fields(CogCard)},
    "additionalProperties": True,
}
"""The OpenAPI schema of the card: every key the reader emits, documented, none type-enforced."""

CardDocument = Annotated[dict[str, Any], WithJsonSchema(CARD_SCHEMA)]


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
    card: CardDocument

    @classmethod
    def of(cls, row: CogArtifact) -> CogEntry:
        return cls(**CogVersion.of(row).model_dump(), cog_id=row.cog_id or "", card=row.card or {})


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
