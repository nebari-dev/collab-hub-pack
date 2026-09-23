"""The Cog catalog read API (issue #85): discover, inspect and pin indexed Cogs.

Read-only over the catalog the indexer fills (:mod:`..cogs.catalog`). The
hub never proxies blobs and never contacts a registry here: every answer
comes from what was captured at index time, and an install goes client ->
registry with the pinned reference this API hands out.

- ``GET /v1/cogs`` -- current Cogs, one per ``cog_id`` (its newest present
  version), filtered and paged with ``limit`` + ``offset``.
- ``GET /v1/cogs/{cog_id}`` -- the current card plus every indexed version.
- ``GET /v1/cogs/{cog_id}/versions/{digest}`` -- the card for one digest.
  Removed artifacts stay reachable here (installs pin digests); they are
  hidden only from the listing.
- ``.../versions/{digest}/cog.md`` -- the COG.md Markdown body, as indexed.
- ``.../versions/{digest}/reference`` -- the pinned install reference.
- ``GET /v1/cogs/catalog.v1.json`` -- transitional compatibility view in the
  static ``catalog.v1.json`` shape.

``cog_id`` contains a ``/`` (``<publisher>/<name>``), so it is a ``path``
parameter; the fixed routes are declared before the ``{cog_id:path}`` ones
so ``catalog.v1.json`` is never read as a Cog id. A malformed digest is a
422 (request validation, like every other malformed path value here); a
well-formed digest the catalog does not hold for that Cog is a 404.

Authenticated like the frames routes. Anonymous discovery is not a
``security.paths`` entry alone: the route dependency still requires a
caller, whatever the path map says.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request, status
from fastapi.responses import PlainTextResponse

from ..cogs.catalog import (
    STATUS_INDEXED,
    CatalogFilter,
    CogArtifact,
    CogCatalogStore,
    CogCatalogUnavailableError,
)
from ..cogs.models import (
    CatalogV1,
    CatalogV1Repository,
    CogDetail,
    CogEntry,
    CogErrorResponse,
    CogListPage,
    CogLocation,
    CogReference,
    CogVersion,
)
from ..dependencies import get_cog_catalog_store
from ..frames.auth import AuthContext, get_auth_context
from .frames import error_response

router = APIRouter(prefix="/cogs", tags=["cogs"])

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
CatalogDep = Annotated[CogCatalogStore, Depends(get_cog_catalog_store)]

DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
"""The only digest form the indexer stores (the OCI client accepts sha256 alone)."""

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
FILTER_MAX_LENGTH = 512

CogIdPath = Annotated[str, Path(description="The Cog id, `<publisher>/<name>`; its `/` is part of the path.")]
DigestPath = Annotated[str, Path(pattern=DIGEST_PATTERN, description="`sha256:` followed by 64 lowercase hex digits.")]


def _filter(description: str) -> object:
    return Query(min_length=1, max_length=FILTER_MAX_LENGTH, description=description)


NOT_FOUND = {404: {"model": CogErrorResponse, "description": "No such Cog, or no such version of it."}}
UNAVAILABLE = {503: {"model": CogErrorResponse, "description": "No catalog storage is configured."}}


class CogNotFoundError(LookupError):
    """No current version of this Cog is indexed."""


class CogVersionNotFoundError(LookupError):
    """This digest is not indexed as a version of this Cog."""


def _location_key(row: CogArtifact) -> tuple:
    # Present before removed, then newest pushed (unknown last), then most
    # recently indexed, then a stable tiebreak -- the catalog's own order.
    pushed = row.pushed_at.timestamp() if row.pushed_at else 0.0
    indexed = row.indexed_at.timestamp() if row.indexed_at else 0.0
    return (row.removed_at is not None, row.pushed_at is None, -pushed, -indexed, row.source_id, row.repository)


def _version_locations(store: CogCatalogStore, cog_id: str, digest: str) -> list[CogArtifact]:
    """Every indexed location of ``digest`` as a version of ``cog_id``, preferred first; 404 when none."""

    rows = [row for row in store.locations(digest) if row.status == STATUS_INDEXED and row.cog_id == cog_id]
    if not rows:
        raise CogVersionNotFoundError(cog_id)
    return sorted(rows, key=_location_key)


@router.get(
    "",
    response_model=CogListPage,
    responses=UNAVAILABLE,
    summary="List current Cogs",
)
def list_cogs(
    _auth: AuthDep,
    store: CatalogDep,
    kind: Annotated[str | None, _filter("The card's `kind` (e.g. `complete`, `model`, `context`).")] = None,
    publisher: Annotated[str | None, _filter("The card's `publisher`, exactly.")] = None,
    provides: Annotated[str | None, _filter("An entry of the card's `provides`.")] = None,
    requires: Annotated[str | None, _filter("A capability named in the card's `requires`.")] = None,
    accepts: Annotated[str | None, _filter("An io type in the card's `io.accepts`.")] = None,
    produces: Annotated[str | None, _filter("An io type in the card's `io.produces`.")] = None,
    q: Annotated[str | None, _filter("Case-insensitive substring of the Cog's name or description.")] = None,
    source_id: Annotated[
        str | None, _filter("Only this registry source; the newest version is chosen within it.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CogListPage:
    """One entry per `cog_id`: its newest present version, ordered by `cog_id`.

    `source_id` scopes which versions are considered; every other filter
    tests that newest version, so an entry is always the card
    `GET /v1/cogs/{cog_id}` serves. Removed artifacts, non-Cog artifacts and
    failed reads never appear.
    """

    filters = CatalogFilter(
        kind=kind,
        publisher=publisher,
        source_id=source_id,
        provides=provides,
        requires=requires,
        accepts=accepts,
        produces=produces,
        q=q,
    )
    # One row past the page says whether another page exists, without a
    # count query and without an empty trailing request.
    rows = store.list_current(filters, limit=limit + 1, offset=offset)
    return CogListPage(
        items=[CogEntry.of(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.get(
    "/catalog.v1.json",
    response_model=CatalogV1,
    responses=UNAVAILABLE,
    summary="Transitional catalog.v1.json view",
)
def catalog_v1(_auth: AuthDep, store: CatalogDep) -> CatalogV1:
    """**Transitional.** The index in the static `catalog.v1.json` shape, for clients that still read it.

    One entry per repository path holding a present, indexed Cog, sorted and
    deduplicated across sources: `namespace` is the first path segment,
    `name` the rest, `description` the newest version's card description.
    The shape carries no registry host and no digest; new clients use
    `GET /v1/cogs`. A single-segment repository path has no namespace and is
    left out.
    """

    repositories = []
    for row in store.list_repositories():
        namespace, _, name = row.repository.partition("/")
        if not name:
            continue
        description = (row.card or {}).get("description")
        repositories.append(
            CatalogV1Repository(
                namespace=namespace,
                name=name,
                description=description if isinstance(description, str) else "",
            )
        )
    return CatalogV1(repositories=repositories)


@router.get(
    "/{cog_id:path}/versions/{digest}/cog.md",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}, "description": "The COG.md body."},
        **NOT_FOUND,
        **UNAVAILABLE,
    },
    summary="COG.md body of one version",
)
def get_cog_md(_auth: AuthDep, store: CatalogDep, cog_id: CogIdPath, digest: DigestPath) -> PlainTextResponse:
    """The Markdown body of the version's `COG.md` (after its frontmatter), as captured at index time.

    Never fetched live. The frontmatter itself is on the card, parsed
    (`frontmatter`) and verbatim (`frontmatter_raw`).
    """

    card = _version_locations(store, cog_id, digest)[0].card or {}
    body = card.get("body")
    return PlainTextResponse(body if isinstance(body, str) else "", media_type="text/markdown; charset=utf-8")


@router.get(
    "/{cog_id:path}/versions/{digest}/reference",
    response_model=CogReference,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="Pinned install reference of one version",
)
def get_cog_reference(_auth: AuthDep, store: CatalogDep, cog_id: CogIdPath, digest: DigestPath) -> CogReference:
    """`<host>/<repository>@<digest>`, ready for `nebi import`.

    When the digest was indexed in several sources or repositories, the
    newest-pushed present location is the `reference` and the others are
    listed under `locations`. A digest whose every location was removed is
    still answered (`present: false`): the artifact may already be gone from
    its registry.
    """

    preferred, *others = _version_locations(store, cog_id, digest)
    return CogReference(
        reference=preferred.reference,
        source_id=preferred.source_id,
        repository=preferred.repository,
        digest=preferred.digest,
        present=preferred.present,
        locations=[CogLocation.of(row) for row in others],
    )


@router.get(
    "/{cog_id:path}/versions/{digest}",
    response_model=CogEntry,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="Card of one version",
)
def get_cog_version(_auth: AuthDep, store: CatalogDep, cog_id: CogIdPath, digest: DigestPath) -> CogEntry:
    """The exact card indexed for this digest -- what an install pins.

    Removed versions are served too, with `removed_at` set.
    """

    return CogEntry.of(_version_locations(store, cog_id, digest)[0])


@router.get(
    "/{cog_id:path}",
    response_model=CogDetail,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="A Cog and its versions",
)
def get_cog(_auth: AuthDep, store: CatalogDep, cog_id: CogIdPath) -> CogDetail:
    """The Cog's current card (its newest present version) and every indexed version, newest first.

    404 when no version is present, even if removed ones are indexed: those
    stay reachable by digest.
    """

    versions = store.list_versions(cog_id, include_removed=True)
    current = next((row for row in versions if row.present), None)
    if current is None:
        raise CogNotFoundError(cog_id)
    return CogDetail(**CogEntry.of(current).model_dump(), versions=[CogVersion.of(row) for row in versions])


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CogNotFoundError)
    async def cog_not_found_handler(_request: Request, _exc: CogNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "cog_not_found", "Cog not found")

    @app.exception_handler(CogVersionNotFoundError)
    async def cog_version_not_found_handler(_request: Request, _exc: CogVersionNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "cog_version_not_found", "Cog version not found")

    @app.exception_handler(CogCatalogUnavailableError)
    async def cog_catalog_unavailable_handler(_request: Request, exc: CogCatalogUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "cog_catalog_unavailable", str(exc))
