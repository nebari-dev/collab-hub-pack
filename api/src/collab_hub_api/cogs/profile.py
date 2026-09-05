"""Profile manifest loading and catalog-card derivation for the 0.1 Cog profile.

``COG.md`` says *where* the manifest is (``manifest``) and *what it claims to
be* (``manifest_schema``); this module follows the pointer and reads the one
profile the hub understands, ``openteams/cog-manifest [0.1]``. That profile
lives in one of two files today and the pointer decides which:

- ``pixi.toml`` under ``[tool.cog]`` -- the default. Per ADR-0001 D9 the
  profile puts its declarations in the one file the package manager already
  reads, so ``version`` and the summary are stated once: ``[tool.cog]`` may
  omit them and they come from ``[workspace]`` (``version`` / ``description``;
  ``[project]`` is the legacy table name).
- ``cog.yaml`` -- the standalone YAML manifest. Its pixi tasks still come
  from a sibling ``pixi.toml`` when there is one, because the card's ``ops``
  are the package's runnable tasks whichever file holds the profile.

The card derivation is a port of the Cog build tooling's ``card --json``
command. The hub and the CLI must agree on what a Cog's card says, so the
rules here are that tool's, not a second schema: declared audience beats the
lifecycle-name fallback, an endpoint-bearing interface's task is lifecycle,
leftover pixi tasks are classified by the same name set, and the ``model``
block exists only for ``kind: model``.

Everything the profile declares is also kept whole (``Profile.data``) so
``requires[].credential``, ``interfaces[].served_model_id``, ``model.weights``
and whatever a later profile adds survive un-flattened (D9/D10). The card is
a view; the profile is the record.
"""

from __future__ import annotations

import datetime
import math
import posixpath
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from .frontmatter import FrontmatterDocument

PROFILE_SCHEMA_V01 = "openteams/cog-manifest [0.1]"
# One published Cog's frontmatter names the profile ``cog-package`` while its
# manifest says ``cog-manifest``; both identify the same 0.1 profile. Readers
# accept either and note the disagreement rather than refuse the Cog.
PROFILE_SCHEMA_V01_ALIAS = "openteams/cog-package [0.1]"
KNOWN_PROFILE_SCHEMAS = frozenset({PROFILE_SCHEMA_V01, PROFILE_SCHEMA_V01_ALIAS})

# The older Prog shape: no COG.md, capabilities declared in pixi.toml under
# ``[tool.nebi.capability]`` (spec-version 0.1.0). Read so the same index can
# carry Progs without a second reader.
PROG_PROFILE_SCHEMA = "nebi/capability [0.1.0]"
PROG_SPEC_VERSION = "0.1.0"
PROG_TABLE_PATH = ("tool", "nebi", "capability")

PIXI_TOOL_TABLE = "cog"
PIXI_MANIFEST = "pixi.toml"

# Conventional lifecycle tasks: the FALLBACK classification when an interface
# declares no explicit ``audience`` (declaration beats inference).
LIFECYCLE_TASKS = frozenset({"resolve", "use", "check", "eval", "test", "bundle", "serve"})

# A Cog built on the shared machinery carries this file; its presence is what
# the build tool reads as "speaks envelope v1". Checked against the bundle's
# path list, never fetched.
ENVELOPE_MARKER = "src/cog_core.py"

PROFILE_PARSED = "parsed"
PROFILE_DRAFT = "draft"
PROFILE_UNPARSED = "unparsed"
PROFILE_MISSING = "missing"


@dataclass
class Profile:
    """What following the ``manifest`` pointer produced.

    ``status`` is one of ``parsed`` (the profile was read), ``draft`` (no
    manifest declared), ``missing`` (declared but not in the bundle, or the
    pointer is unusable) or ``unparsed`` (the file is there but this reader
    cannot or does not read it: unknown schema, no schema, wrong format,
    parse error). ``data`` is the full profile as JSON-shaped data; ``tasks``
    the package's pixi tasks, which the card classifies into ``ops``.
    """

    status: str
    manifest: str | None = None
    schema: str | None = None
    data: dict[str, Any] | None = None
    raw: str = ""
    tasks: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_profile(doc: FrontmatterDocument, files: Mapping[str, bytes]) -> Profile:
    """Follow the document's manifest pointer into ``files`` and read the profile."""

    declared = doc.fields.get("manifest") is not None or doc.fields.get("manifest_schema") is not None
    if not declared:
        return Profile(PROFILE_DRAFT)
    if doc.manifest_path is None:
        # The pointer is absent, non-string, absolute or escaping; the
        # frontmatter reader already said which.
        return Profile(PROFILE_MISSING)

    name = posixpath.basename(doc.manifest_path)
    data = files.get(doc.manifest_path)
    if data is None:
        return Profile(
            PROFILE_MISSING, manifest=name, errors=[f"manifest file is not in the bundle: {doc.manifest_path}"]
        )

    raw, err = _decode(data, doc.manifest_path)
    if err:
        return Profile(PROFILE_UNPARSED, manifest=name, errors=[err])
    if doc.manifest_schema is None:
        # Reported by the frontmatter reader; without a schema there is nothing
        # to dispatch on, and guessing from the file name is how two readers
        # come to disagree about what a Cog is.
        return Profile(PROFILE_UNPARSED, manifest=name, raw=raw)
    if doc.manifest_schema not in KNOWN_PROFILE_SCHEMAS:
        return Profile(
            PROFILE_UNPARSED,
            manifest=name,
            raw=raw,
            warnings=[f"manifest_schema {doc.manifest_schema!r} is not a profile this reader understands"],
        )

    suffix = posixpath.splitext(name)[1].lower()
    tasks: dict[str, Any] = {}
    warnings: list[str] = []
    if suffix == ".toml":
        profile, tasks, err = _profile_from_pixi(raw)
    elif suffix in (".yaml", ".yml"):
        profile, err = _profile_from_yaml(raw)
        pixi = files.get(PIXI_MANIFEST)
        if pixi is not None:
            tasks, task_err = _tasks_from_pixi(pixi)
            if task_err:
                warnings.append(f"{PIXI_MANIFEST} tasks not read: {task_err}")
    else:
        profile, err = None, f"manifest {name!r} is neither TOML nor YAML; the 0.1 profile lives in one of those"
    if err or profile is None:
        return Profile(PROFILE_UNPARSED, manifest=name, raw=raw, errors=[err or "profile is empty"], warnings=warnings)

    schema = profile.get("schema")
    schema = schema if isinstance(schema, str) else None
    if schema is not None and schema != doc.manifest_schema:
        note = "; both name the 0.1 profile" if schema in KNOWN_PROFILE_SCHEMAS else ""
        warnings.append(
            f"frontmatter manifest_schema {doc.manifest_schema!r} and profile schema {schema!r} disagree{note}"
        )
    return Profile(PROFILE_PARSED, manifest=name, schema=schema, data=profile, raw=raw, tasks=tasks, warnings=warnings)


def _decode(data: bytes, path: str) -> tuple[str, str | None]:
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", f"{path} is not valid UTF-8"


def load_pixi_document(data: bytes) -> tuple[str, dict[str, Any] | None, str | None]:
    """Decode and parse ``pixi.toml``. Return ``(text, document, error)``; never raises."""

    raw, err = _decode(data, PIXI_MANIFEST)
    if err:
        return raw, None, err
    try:
        return raw, tomllib.loads(raw), None
    except Exception as exc:  # noqa: BLE001 -- see below
        # ``TOMLDecodeError`` is the documented failure, but a parser's failure
        # modes are not enumerable from outside it (deep nesting recurses), and
        # the contract here is a card with errors, never a traceback.
        return raw, None, f"{PIXI_MANIFEST} is not valid TOML: {exc}"


def _tasks_from_pixi(data: bytes) -> tuple[dict[str, Any], str | None]:
    _, doc, err = load_pixi_document(data)
    if doc is None:
        return {}, err
    tasks = doc.get("tasks")
    return (jsonable(tasks) if isinstance(tasks, dict) else {}), None


def _profile_from_pixi(raw: str) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    """``[tool.cog]`` with the stated-once fallbacks applied, plus ``[tasks]``."""

    try:
        doc = tomllib.loads(raw)
    except Exception as exc:  # noqa: BLE001 -- same reasoning as load_pixi_document
        return None, {}, f"manifest is not valid TOML: {exc}"
    tool = doc.get("tool")
    table = tool.get(PIXI_TOOL_TABLE) if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        return None, {}, f"manifest has no [tool.{PIXI_TOOL_TABLE}] table"
    profile = dict(table)
    workspace = doc.get("workspace") or doc.get("project") or {}
    if not isinstance(workspace, dict):
        workspace = {}
    if "version" not in profile and workspace.get("version") is not None:
        profile["version"] = workspace["version"]
    if "summary" not in profile and workspace.get("description") is not None:
        profile["summary"] = workspace["description"]
    tasks = doc.get("tasks")
    return jsonable(profile), (jsonable(tasks) if isinstance(tasks, dict) else {}), None


def _profile_from_yaml(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        loaded = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 -- same reasoning as load_pixi_document
        return None, f"manifest is not valid YAML: {exc}"
    if not isinstance(loaded, dict):
        return None, "manifest must be a YAML mapping"
    return jsonable(loaded), None


def jsonable(value: Any) -> Any:
    """Reduce parser output to JSON-shaped data.

    TOML and YAML both construct values JSON has no spelling for (dates,
    times, bytes, sets, non-string keys, non-finite floats). The card is
    stored as ``jsonb`` and served as JSON, so those are folded here once
    rather than at every consumer.
    """

    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def derive_card_fields(
    profile: Mapping[str, Any],
    tasks: Mapping[str, Any],
    manifest: str | None,
    bundle_paths: Collection[str],
) -> tuple[dict[str, Any], list[str]]:
    """The build tool's card, computed from a parsed 0.1 profile.

    Returns the card's build-tool keys and any warnings about declarations
    the card had to skip. The tool assumes well-formed input; this port skips
    a malformed entry (an interface that is not a mapping, a task name that
    is not a string) and says so, because the alternative is a traceback for
    the whole bundle over one bad line.
    """

    warnings: list[str] = []
    interfaces = _mapping_items(profile.get("interfaces"), "interfaces", warnings)

    usage: set[str] = set()
    lifecycle: set[str] = set()
    inferred = False
    for iface in interfaces:
        task = iface.get("task")
        if not isinstance(task, str) or not task:
            continue
        if iface.get("endpoint"):
            # The ENDPOINT is the usage surface; its task starts the service.
            lifecycle.add(task)
            continue
        audience = iface.get("audience")
        if audience == "usage":
            usage.add(task)
        elif audience == "lifecycle":
            lifecycle.add(task)
        elif task in LIFECYCLE_TASKS:
            lifecycle.add(task)
            inferred = True
        else:
            usage.add(task)
            inferred = True
    for task in tasks:
        if task not in usage and task not in lifecycle:
            (lifecycle if task in LIFECYCLE_TASKS else usage).add(task)
            inferred = True

    requires = []
    for req in _mapping_items(profile.get("requires"), "requires", warnings):
        also = req.get("also_satisfied_by")
        satisfiers = [req.get("satisfied_by") or {}] + (also if isinstance(also, list) else [])
        requires.append(
            {
                "capability": req.get("capability"),
                "locality": req.get("locality", "any"),
                "satisfiers": [s.get("cog") for s in satisfiers if isinstance(s, dict) and s.get("cog")],
            }
        )

    context = _mapping_or_empty(profile.get("context"), "context", warnings)
    model = _mapping_or_empty(profile.get("model"), "model", warnings)
    evaluation = _mapping_or_empty(profile.get("evaluation"), "evaluation", warnings)
    default = next((iface for iface in interfaces if iface.get("default")), {})

    fields = {
        "card": 1,
        "manifest": manifest,
        "audience_inferred": inferred,
        "provides": profile.get("provides") or [],
        "locality": profile.get("locality"),
        "model": (
            {
                "name": model.get("name"),
                "quantization": model.get("quantization"),
                "runtime": model.get("runtime"),
                "revision": model.get("revision"),
                "served_model_id": default.get("served_model_id"),
                "address": default.get("address") or default.get("endpoint"),
            }
            if profile.get("kind") == "model"
            else None
        ),
        "id": profile.get("id"),
        "version": profile.get("version"),
        "kind": profile.get("kind"),
        "summary": collapse_whitespace(profile.get("summary")),
        "owner": profile.get("owner"),
        "license": profile.get("license"),
        "io": profile.get("io"),
        "entry_points": [
            {
                "name": iface.get("name"),
                "kind": iface.get("kind"),
                "task": iface.get("task"),
                "audience": iface.get("audience"),
                "endpoint": iface.get("endpoint"),
                "default": bool(iface.get("default")),
            }
            for iface in interfaces
        ],
        "ops": {"usage": sorted(usage), "lifecycle": sorted(lifecycle)},
        "requires": requires,
        "prohibits": profile.get("prohibits") or [],
        "input_contract": context.get("input_schema"),
        "output_contract": context.get("output_schema"),
        "envelope": envelope_version(bundle_paths),
        "fixtures": evaluation.get("fixtures") or [],
    }
    return fields, warnings


def envelope_version(bundle_paths: Collection[str]) -> int | None:
    return 1 if ENVELOPE_MARKER in bundle_paths else None


def collapse_whitespace(value: Any) -> str:
    """The card's one-line summary: any whitespace run becomes one space."""

    return " ".join(str(value or "").split())


def _mapping_items(value: Any, key: str, warnings: list[str]) -> list[dict[str, Any]]:
    """The mapping entries of a declared list, skipping (and noting) anything else."""

    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append(f"profile {key!r} is not a list; ignored")
        return []
    items = [item for item in value if isinstance(item, dict)]
    if len(items) != len(value):
        warnings.append(f"profile {key!r} has {len(value) - len(items)} entr(y/ies) that are not mappings; skipped")
    return items


def _mapping_or_empty(value: Any, key: str, warnings: list[str]) -> dict[str, Any]:
    if value is None or isinstance(value, dict):
        return value or {}
    warnings.append(f"profile {key!r} is not a mapping; ignored")
    return {}


def prog_capabilities(
    pixi_doc: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[tuple[str, str, dict[str, Any]]]]:
    """The ``[tool.nebi.capability]`` table and its ``(org, key, table)`` entries.

    Returns ``(None, [])`` when the document declares no capability table.
    Capabilities are keyed two levels deep (``<org>.<key>``); anything at
    those levels that is not a table (``spec-version`` at the top, say) is
    not a capability.
    """

    node: Any = pixi_doc
    for part in PROG_TABLE_PATH:
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return None, []
    entries = []
    for org, keys in node.items():
        if not isinstance(keys, dict):
            continue
        for key, table in keys.items():
            if isinstance(table, dict):
                entries.append((str(org), str(key), table))
    return node, entries
