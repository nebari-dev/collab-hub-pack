"""Turn the files of a published Cog bundle into its catalog card.

This is the registry-free half of indexing (issue #81): the caller has
already fetched the bundle's small files -- ``COG.md``, the manifest it names,
usually ``pixi.toml`` -- and knows which paths the bundle contains from its
OCI layer titles. ``read_cog_bundle`` takes those and nothing else. No
network, no registry, no HTTP: the same function reads a bundle from a
directory listing, a test fixture, or a registry client, and the catalog
entry is the Cog's own declarations, not a hub-invented schema (ADR-0001 D9).

The card has two halves. The build-tool keys (``card`` through ``fixtures``)
are exactly what the Cog build tooling's ``card --json`` prints, so the hub
and the CLI never disagree about what a Cog is. The hub-side keys carry what
an index needs beyond that: the frontmatter identity, the whole profile as
structured data, the Markdown body, and every problem the reader found.

**Frontmatter is authoritative for identity, and the profile must agree.**
When a parsed profile's ``id`` does not end in the frontmatter ``name``, or
its ``version`` differs from the frontmatter ``version``, the card records a
``conflict:`` error and shows both values (frontmatter under ``frontmatter``,
profile under ``profile`` and the build-tool keys). It does not pick a winner:
a catalog that quietly preferred one side would hide exactly the publishing
mistake a reader is best placed to surface.

**The reader never raises.** Malformed input of every kind -- invalid UTF-8,
frontmatter outside the spec subset, a manifest that is not TOML, a profile
whose ``interfaces`` is a string -- yields a card whose ``errors`` say what
went wrong. An indexer sweeping a registry must record one bad bundle and
move on, and the only way to guarantee that from here is to make it
impossible to leave this module by exception.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .frontmatter import FrontmatterDocument, read_cog_document
from .profile import (
    PIXI_MANIFEST,
    PROFILE_PARSED,
    PROFILE_UNPARSED,
    PROG_PROFILE_SCHEMA,
    PROG_SPEC_VERSION,
    Profile,
    collapse_whitespace,
    derive_card_fields,
    envelope_version,
    jsonable,
    load_pixi_document,
    load_profile,
    prog_capabilities,
)

COG_ENTRY_FILE = "COG.md"
PROG_KIND = "prog"


@dataclass
class CogCard:
    """One Cog's catalog card. ``to_dict()`` is the JSON the store keeps and the API serves.

    Field order is the card schema: first the build-tool keys in the order
    ``card --json`` prints them, then the hub-side keys. When the profile was
    not parsed the build-tool keys are empty except that ``version``, ``kind``
    and ``license`` fall back to the frontmatter, so a draft or an unreadable
    Cog still lists with the identity its author wrote.
    """

    card: int = 1
    manifest: str | None = None
    audience_inferred: bool = False
    provides: Any = field(default_factory=list)
    locality: Any = None
    model: dict[str, Any] | None = None
    id: str | None = None
    version: Any = None
    kind: Any = None
    summary: str = ""
    owner: Any = None
    license: Any = None
    io: Any = None
    entry_points: list[dict[str, Any]] = field(default_factory=list)
    ops: dict[str, list[str]] = field(default_factory=lambda: {"usage": [], "lifecycle": []})
    requires: list[dict[str, Any]] = field(default_factory=list)
    prohibits: Any = field(default_factory=list)
    input_contract: Any = None
    output_contract: Any = None
    envelope: int | None = None
    fixtures: Any = field(default_factory=list)

    name: str | None = None
    description: str | None = None
    publisher: str | None = None
    manifest_schema: str | None = None
    profile_status: str = "missing"
    profile_schema: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    frontmatter_raw: str = ""
    profile: dict[str, Any] | None = None
    profile_raw: str = ""
    body: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_cog_bundle(files: Mapping[str, bytes], *, bundle_paths: Collection[str] | None = None) -> CogCard:
    """Read a bundle's files into its ``CogCard``. Pure; never raises.

    ``files`` maps bundle-relative paths to the bytes that were fetched.
    ``bundle_paths`` is every path the bundle contains (from the OCI layer
    titles), so the reader can answer "is ``src/cog_core.py`` there" without
    anyone fetching it; when omitted the fetched files stand in for the list.
    """

    try:
        return _read(files, frozenset(bundle_paths if bundle_paths is not None else files))
    except Exception as exc:  # noqa: BLE001 -- the contract is a card, never a traceback
        # Every anticipated failure is handled where it happens and lands in
        # ``errors`` with context. This guard is for the unanticipated one, so
        # that a single bundle nobody has seen the shape of yet cannot stop an
        # index sweep. A test forces it by faulting the reader itself.
        return CogCard(errors=[f"reader error: {type(exc).__name__}: {exc}"])


def _read(files: Mapping[str, bytes], bundle_paths: frozenset[str]) -> CogCard:
    envelope = envelope_version(bundle_paths)
    entry = files.get(COG_ENTRY_FILE)
    if entry is None:
        return _read_prog(files, envelope)

    doc = read_cog_document(entry)
    profile = load_profile(doc, files)
    card = CogCard(
        manifest=profile.manifest,
        envelope=envelope,
        name=_string(doc.fields, "name"),
        description=_string(doc.fields, "description"),
        publisher=_string(doc.fields, "publisher"),
        manifest_schema=doc.manifest_schema,
        profile_status=profile.status,
        profile_schema=profile.schema,
        frontmatter=doc.fields,
        frontmatter_raw=doc.raw,
        profile=profile.data,
        profile_raw=profile.raw,
        body=doc.body,
        errors=[*doc.errors, *profile.errors],
        warnings=list(profile.warnings),
    )
    if profile.status == PROFILE_PARSED and profile.data is not None:
        fields, warnings = derive_card_fields(profile.data, profile.tasks, profile.manifest, envelope)
        for key, value in fields.items():
            setattr(card, key, value)
        card.warnings.extend(warnings)
        errors, warnings = _agreement(doc, profile)
        card.errors.extend(errors)
        card.warnings.extend(warnings)
    else:
        card.version = _string(doc.fields, "version")
        card.kind = _string(doc.fields, "kind")
        card.license = _string(doc.fields, "license")
    return card


def _agreement(doc: FrontmatterDocument, profile: Profile) -> tuple[list[str], list[str]]:
    """The conflict rule: the profile must agree with the frontmatter on identity.

    ``name`` against the last path segment of the profile ``id``
    (``example/my-cog`` names ``my-cog``) and ``version`` against ``version``
    are errors, because a catalog cannot list a Cog under two names or two
    versions. ``kind`` is a warning: the frontmatter's is the spec's coarse
    vocabulary and the profile's is what the card shows, so a disagreement
    is worth a look but does not make the Cog unlistable.
    """

    data = profile.data or {}
    errors = []
    warnings = []
    name = doc.fields.get("name")
    cog_id = data.get("id")
    if isinstance(name, str) and isinstance(cog_id, str) and cog_id.rsplit("/", 1)[-1] != name:
        errors.append(f"conflict: profile id {cog_id!r} does not end with frontmatter name {name!r}")
    version = doc.fields.get("version")
    profile_version = data.get("version")
    if isinstance(version, str) and profile_version is not None and str(profile_version) != version:
        errors.append(f"conflict: profile version {profile_version!r} disagrees with frontmatter version {version!r}")
    kind = doc.fields.get("kind")
    profile_kind = data.get("kind")
    if isinstance(kind, str) and profile_kind is not None and profile_kind != kind:
        warnings.append(f"profile kind {profile_kind!r} disagrees with frontmatter kind {kind!r}")
    return errors, warnings


def _read_prog(files: Mapping[str, bytes], envelope: int | None) -> CogCard:
    """A bundle with no ``COG.md``: a Prog if ``pixi.toml`` declares a capability, otherwise not a Cog."""

    card = CogCard(envelope=envelope)
    pixi = files.get(PIXI_MANIFEST)
    if pixi is None:
        card.errors.append(f"bundle has no {COG_ENTRY_FILE}")
        return card
    raw, doc, err = load_pixi_document(pixi)
    table, entries = prog_capabilities(doc or {})
    if table is None:
        card.errors.append(f"bundle has no {COG_ENTRY_FILE} and {PIXI_MANIFEST} declares no [tool.nebi.capability]")
        if err:
            card.errors.append(err)
        return card

    card.kind = PROG_KIND
    card.manifest = PIXI_MANIFEST
    card.profile_schema = PROG_PROFILE_SCHEMA
    card.profile = jsonable(table)
    card.profile_raw = raw
    spec_version = table.get("spec-version")
    if spec_version != PROG_SPEC_VERSION:
        card.warnings.append(f"[tool.nebi.capability] spec-version {spec_version!r} is not {PROG_SPEC_VERSION!r}")
    if not entries:
        card.profile_status = PROFILE_UNPARSED
        card.errors.append("[tool.nebi.capability] declares no <org>.<key> capability table")
        return card
    if len(entries) > 1:
        others = ", ".join(f"{org}/{key}" for org, key, _ in entries[1:])
        card.warnings.append(
            f"pixi.toml declares {len(entries)} capabilities; the card describes the first ({others} kept in profile)"
        )

    org, key, capability = entries[0]
    card.profile_status = PROFILE_PARSED
    card.id = f"{org}/{key}"
    card.name = key
    card.description = _string(capability, "description")
    card.summary = collapse_whitespace(card.description)
    author = capability.get("author")
    card.publisher = _string(author, "name") if isinstance(author, dict) else None
    workspace = (doc or {}).get("workspace") or (doc or {}).get("project") or {}
    card.version = _string(workspace, "version") if isinstance(workspace, dict) else None

    targets = capability.get("targets")
    default_target = capability.get("default-target")
    for target, spec in targets.items() if isinstance(targets, dict) else ():
        if not isinstance(spec, dict):
            continue
        card.entry_points.append(
            {
                "name": str(target),
                "kind": "command",
                "task": _string(spec, "task"),
                "audience": "usage",
                "endpoint": None,
                "default": target == default_target,
            }
        )
    card.ops = {"usage": sorted({ep["task"] for ep in card.entry_points if ep["task"]}), "lifecycle": []}
    return card


def _string(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None
