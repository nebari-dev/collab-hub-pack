from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from collab_hub_api.cogs import bundle as bundle_module
from collab_hub_api.cogs.bundle import CogCard, read_cog_bundle
from collab_hub_api.cogs.frontmatter import read_cog_document
from collab_hub_api.cogs.profile import LIFECYCLE_TASKS, jsonable

FIXTURES = Path(__file__).parent / "fixtures" / "cogs"

# The build tool's `card --json` keys, in the order it prints them. The card's
# top-level dict must start with exactly these so the hub and the CLI agree.
BUILD_TOOL_KEYS = [
    "card",
    "manifest",
    "audience_inferred",
    "provides",
    "locality",
    "model",
    "id",
    "version",
    "kind",
    "summary",
    "owner",
    "license",
    "io",
    "entry_points",
    "ops",
    "requires",
    "prohibits",
    "input_contract",
    "output_contract",
    "envelope",
    "fixtures",
]
HUB_KEYS = [
    "name",
    "description",
    "publisher",
    "manifest_schema",
    "profile_status",
    "profile_schema",
    "frontmatter",
    "frontmatter_raw",
    "profile",
    "profile_raw",
    "body",
    "errors",
    "warnings",
]

# Not part of the bundle: the OCI layer list and the pinned snapshot.
_FIXTURE_METADATA = {"bundle-paths.txt", "expected-card.json"}


def load_fixture(name: str) -> tuple[dict[str, bytes], list[str] | None]:
    root = FIXTURES / name
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in _FIXTURE_METADATA
    }
    paths_file = root / "bundle-paths.txt"
    paths = paths_file.read_text().split() if paths_file.exists() else None
    return files, paths


def read_fixture(name: str) -> CogCard:
    files, paths = load_fixture(name)
    return read_cog_bundle(files, bundle_paths=paths)


def cog_md(frontmatter: str, body: str = "\n# Body\n") -> bytes:
    return f"---\n{frontmatter}\n---\n{body}".encode()


MINIMAL = "type: cog [0.1]\nname: sample-worker\ndescription: A sample worker."


# ---------------------------------------------------------------------------
# Card schema
# ---------------------------------------------------------------------------


def test_card_keys_are_the_build_tool_keys_then_the_hub_keys():
    card = read_fixture("pixi-context").to_dict()

    assert list(card) == BUILD_TOOL_KEYS + HUB_KEYS


def test_pixi_context_card_matches_the_pinned_snapshot():
    expected = json.loads((FIXTURES / "pixi-context" / "expected-card.json").read_text())

    card = read_fixture("pixi-context").to_dict()

    assert card == expected
    # The snapshot is what the store keeps: it must round-trip through JSON.
    assert json.loads(json.dumps(card)) == expected


def test_to_dict_is_a_copy():
    card = read_fixture("pixi-context")
    card.to_dict()["ops"]["usage"].append("mutated")

    assert card.ops["usage"] == ["ask"]


def test_reader_imports_no_network_modules():
    """Acceptance 3: no registry or HTTP module anywhere in the reader."""

    forbidden = {"httpx", "urllib", "requests", "socket", "http", "aiohttp", "ssl"}
    for module in ("bundle", "frontmatter", "profile"):
        source = (Path(bundle_module.__file__).parent / f"{module}.py").read_text()
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{module}.py imports {name}"


# ---------------------------------------------------------------------------
# Fixture shapes
# ---------------------------------------------------------------------------


def test_pixi_context_cog_derives_ops_requires_and_envelope():
    card = read_fixture("pixi-context")

    assert card.profile_status == "parsed"
    assert card.errors == [] and card.warnings == []
    assert card.id == "example/cog-meeting-notes"
    assert card.name == "cog-meeting-notes"
    # Stated once in [workspace]: version and summary fall back.
    assert card.version == "0.1.0"
    assert card.summary == "Extracts grounded, cited notes from meeting records for a stated focus."
    # Endpoint-bearing interface's task is lifecycle; undeclared audiences and
    # leftover pixi tasks are inferred by the lifecycle-name set.
    assert card.ops == {"usage": ["ask"], "lifecycle": ["check", "eval", "resolve", "serve", "test", "use"]}
    assert card.audience_inferred is True
    assert card.requires == [
        {
            "capability": "model-endpoint/openai-compatible",
            "locality": "any",
            "satisfiers": ["example/cog-small-model", "example/cog-other-model"],
        }
    ]
    assert card.model is None
    assert card.fixtures == ["evals/smoke.fixture.yaml"]
    assert card.input_contract == "context/input-schema.json"
    # src/cog_core.py is in the layer list but was never fetched.
    assert card.envelope == 1
    assert card.frontmatter["metadata"] == {"category": "meetings", "maturity": "experimental"}
    assert card.frontmatter_raw.startswith("type: cog [0.1]\n")
    assert card.body.startswith("\n# Meeting Notes")
    # The profile is kept whole: the satisfier's version constraint survives.
    assert card.profile["requires"][0]["satisfied_by"]["version"] == ">=0.1.0"
    assert card.profile["context"]["instructions"] == "context/system.md"
    assert card.profile_raw.startswith("[workspace]")


def test_pixi_complete_cog_keeps_model_table_in_profile_but_not_on_card():
    card = read_fixture("pixi-complete")

    assert card.kind == "complete"
    assert card.model is None
    assert card.profile["model"]["revision"] == "abc1234"
    # Declared audiences are honoured; only the leftover `test` task is inferred.
    assert card.ops == {"usage": ["transcribe"], "lifecycle": ["check", "test"]}
    assert card.audience_inferred is True
    # The workspace description had a run of spaces and a newline.
    assert card.summary == "Extracts audio from local media and emits timestamped transcript artifacts."
    # requires[].credential is not on the card but is preserved in the profile.
    assert card.requires[0] == {"capability": "model-registry/example", "locality": "any", "satisfiers": []}
    assert card.profile["requires"][0]["credential"] == {"api_key_env": "EXAMPLE_TOKEN"}
    assert card.errors == [] and card.warnings == []


def test_yaml_model_cog_builds_model_block_and_accepts_the_schema_alias():
    card = read_fixture("yaml-model")

    assert card.profile_status == "parsed"
    assert card.manifest == "cog.yaml"
    assert card.kind == "model"
    assert card.provides == ["model-endpoint/openai-compatible", "model-artifact/gguf"]
    assert card.model == {
        "name": "Small-Instruct",
        "quantization": "Q4_K_M",
        "runtime": "example-runtime",
        "revision": None,
        "served_model_id": "small-instruct-q4_k_m",
        "address": "http://127.0.0.1:8080/v1",
    }
    # Tasks come from the sibling pixi.toml even though the profile is YAML.
    assert card.ops == {"usage": ["fetch"], "lifecycle": ["check", "serve"]}
    assert card.io is None
    assert card.profile["model"]["weights"]["sha256"] == "ab12" * 16
    assert card.manifest_schema == "openteams/cog-package [0.1]"
    assert card.profile_schema == "openteams/cog-manifest [0.1]"
    assert card.errors == []
    assert card.warnings == [
        "frontmatter manifest_schema 'openteams/cog-package [0.1]' and profile schema "
        "'openteams/cog-manifest [0.1]' disagree; both name the 0.1 profile"
    ]


def test_draft_is_a_frontmatter_only_card_without_errors():
    card = read_fixture("draft")

    assert card.profile_status == "draft"
    assert card.errors == [] and card.warnings == []
    assert card.manifest is None and card.manifest_schema is None
    assert card.id is None and card.profile is None and card.profile_raw == ""
    # Identity the author wrote still lists.
    assert (card.name, card.version, card.kind, card.license) == (
        "repository-risk-analyst",
        "0.0.1",
        "context",
        "Apache-2.0",
    )
    assert card.publisher == "Example Organization"
    assert card.frontmatter["tags"] == ["risk", "dependencies"]
    assert card.summary == "" and card.ops == {"usage": [], "lifecycle": []}


def test_version_conflict_is_an_error_and_neither_side_wins():
    card = read_fixture("version-conflict")

    assert card.profile_status == "parsed"
    assert card.errors == ["conflict: profile version '0.1.0' disagrees with frontmatter version '0.2.0'"]
    assert card.version == "0.1.0"  # the build-tool key stays the profile's
    assert card.frontmatter["version"] == "0.2.0"
    assert card.profile["version"] == "0.1.0"


def test_unknown_manifest_schema_yields_frontmatter_only_card():
    card = read_fixture("unknown-schema")

    assert card.profile_status == "unparsed"
    assert card.errors == []
    assert card.warnings == [
        "manifest_schema 'example.org/other-profile [2.0]' is not a profile this reader understands"
    ]
    assert card.manifest == "manifest.json"
    assert card.profile is None
    assert card.profile_raw == '{"anything": "goes"}\n'
    assert (card.version, card.kind) == ("1.0.0", "context")
    assert card.id is None


def test_missing_manifest_file_is_reported():
    card = read_fixture("missing-manifest")

    assert card.profile_status == "missing"
    assert card.errors == ["manifest file is not in the bundle: cog.yaml"]
    assert card.manifest == "cog.yaml"
    assert card.name == "cog-forgot-manifest"


def test_prog_bundle_yields_a_minimal_prog_card():
    card = read_fixture("prog")

    assert card.kind == "prog"
    assert card.profile_status == "parsed"
    assert card.profile_schema == "nebi/capability [0.1.0]"
    assert card.manifest == "pixi.toml"
    assert card.id == "example/local-server"
    assert card.name == "local-server"
    assert card.version == "0.3.0"
    assert card.publisher == "Example Organization"
    assert card.summary == "OpenAI-compatible local inference server. Serves on port 8080."
    assert card.entry_points == [
        {"name": "local", "kind": "command", "task": "serve", "audience": "usage", "endpoint": None, "default": True},
        {
            "name": "gpu",
            "kind": "command",
            "task": "serve-gpu",
            "audience": "usage",
            "endpoint": None,
            "default": False,
        },
    ]
    assert card.ops == {"usage": ["serve", "serve-gpu"], "lifecycle": []}
    assert card.profile["example"]["local-server"]["author"]["email"] == "cogs@example.org"
    assert card.profile_raw.startswith("[workspace]")
    assert card.errors == [] and card.warnings == []
    assert card.frontmatter == {} and card.body == ""


def test_fixtures_contain_no_private_data():
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert "@" not in text.replace("cogs@example.org", ""), path


# ---------------------------------------------------------------------------
# Spec corpus (the reference validator's negative fixtures, ported)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected_error"),
    [
        (b"\xff\n", "COG.md is not valid UTF-8"),
        (b"# Missing Frontmatter\n\nNo YAML here.\n", "file does not begin with YAML frontmatter"),
        (cog_md("type: skill\nname: sample-worker\ndescription: A sample."), "'type' value 'skill' is not a valid"),
        (cog_md("type: cog [0.1]\nname: Bad--Name\ndescription: A sample."), "'name' value 'Bad--Name' violates"),
        (cog_md(f"{MINIMAL}\nmetadata:\n  retries: 3"), "'metadata' value for 'retries' is not a string"),
        (cog_md(f"{MINIMAL}\nkind: hybrid"), "'kind' value 'hybrid' is not one of"),
        (
            cog_md("type: cog [0.1]\nname: sample-worker\ndescription: >-\n  Folded across\n  lines."),
            "field 'description': anchors, aliases, tags, and block scalars are outside the supported subset",
        ),
        (
            cog_md("type: cog [0.1]\nname: sample-worker\ndescription: A sample worker: with a mapping indicator."),
            "field 'description': ':' followed by whitespace is a mapping indicator; quote the value",
        ),
        (
            cog_md("type: cog [0.1]\nname: sample-worker\ndescription: @handle reviews evidence."),
            "field 'description': a plain scalar may not begin with '@'; quote the value",
        ),
        (cog_md(f"{MINIMAL}\nmetadata:\n  released: 2001-12-14"), "'metadata' value for 'released' is not a string"),
        (cog_md(f"{MINIMAL}\nmanifest: cog.yaml"), "Cog is missing the required 'manifest_schema' field"),
        (cog_md(f"{MINIMAL}\nmanifest_schema: example.org/x [0.1]"), "Cog is missing the required 'manifest' field"),
    ],
    ids=[
        "01-invalid-utf8",
        "02-missing-frontmatter",
        "03-invalid-type",
        "04-invalid-name",
        "05-non-string-metadata",
        "09-invalid-kind",
        "10-block-scalar-value",
        "11-plain-scalar-mapping-indicator",
        "12-indicator-leading-scalar",
        "13-unquoted-timestamp-metadata",
        "08-missing-manifest-schema",
        "07-missing-manifest",
    ],
)
def test_spec_corpus_case_is_reported(data: bytes, expected_error: str):
    card = read_cog_bundle({"COG.md": data, "cog.yaml": b"id: sample-worker\n"})

    assert any(expected_error in error for error in card.errors), card.errors


def test_escaping_manifest_reference_is_refused():
    card = read_cog_bundle({"COG.md": cog_md(f"{MINIMAL}\nmanifest: ../outside.yaml\nmanifest_schema: x [0.1]")})

    assert card.errors == ["'manifest' escapes the bundle root: ../outside.yaml"]
    assert card.profile_status == "missing"


# ---------------------------------------------------------------------------
# Frontmatter grammar: what the subset admits
# ---------------------------------------------------------------------------


def test_frontmatter_accepts_every_construct_the_subset_allows():
    text = "\n".join(
        [
            "# leading comment",
            "type: cog [0.1]   # trailing comment",
            "name: sample-worker",
            'description: "Tab\\tnew\\nline caf\\u00e9 \\x41 \\U0001F600 quote\\" hash # inside"',
            "version: '0.1.0'",
            "quoted: 'it''s'",
            "urn: urn:example:thing",
            "url: https://example.org/a#frag",
            "dash: -foo",
            "question: ?foo",
            "hash: a#b",
            "x-vendor: yes-ish",
            "example.org/thing: v",
            "flow: [a, 'b c', \"d,e\", f]   # comment",
            "flow_trailing_comma: [a, b, ]",
            'flow_hash: ["a # b", c]',
            "empty_flow: []",
            "block:",
            "  - one",
            "  - 'two'  # note",
            "  # comment between items",
            '  - "three"',
            "nothing:",
            "commented_out: # only a comment",
            "",
            "metadata:",
            "  category: risk",
            "  maturity: 'experimental'",
            "  homepage: https://example.org",
        ]
    )

    doc = read_cog_document(cog_md(text))

    assert doc.errors == []
    assert doc.fields["description"] == 'Tab\tnew\nline café A 😀 quote" hash # inside'
    assert doc.fields["version"] == "0.1.0"
    assert doc.fields["quoted"] == "it's"
    assert doc.fields["urn"] == "urn:example:thing"
    assert doc.fields["url"] == "https://example.org/a#frag"
    assert doc.fields["dash"] == "-foo" and doc.fields["question"] == "?foo" and doc.fields["hash"] == "a#b"
    assert doc.fields["example.org/thing"] == "v"
    assert doc.fields["flow"] == ["a", "b c", "d,e", "f"]
    assert doc.fields["flow_trailing_comma"] == ["a", "b"]
    assert doc.fields["flow_hash"] == ["a # b", "c"]
    assert doc.fields["empty_flow"] == []
    assert doc.fields["block"] == ["one", "two", "three"]
    assert doc.fields["nothing"] is None and doc.fields["commented_out"] is None
    assert doc.fields["metadata"] == {"category": "risk", "maturity": "experimental", "homepage": "https://example.org"}


def test_frontmatter_tolerates_bom_and_crlf():
    data = b"\xef\xbb\xbf---\r\ntype: cog\r\nname: a\r\ndescription: d\r\n---\r\nbody\r\n"

    doc = read_cog_document(data)

    assert doc.errors == []
    assert doc.fields["type"] == "cog"
    assert doc.body == "body\n"


def test_unversioned_type_and_version_bump_are_distinguished():
    assert read_cog_document(cog_md("type: cog\nname: a\ndescription: d")).errors == []
    errors = read_cog_document(cog_md("type: cog [0.2]\nname: a\ndescription: d")).errors
    assert errors == ["'type' value 'cog [0.2]' is not a valid CogSpec v0.1 type (expected 'cog' or 'cog [0.1]')"]


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("type: cog\nname: a\ndescription: d\nversion: 0.1", "'version' must be a string"),
        ("type: cog\nname: a\ndescription: d\nversion: 1e3", "'version' must be a string"),
        ("type: cog\nname: a\ndescription: d\nversion: 12:30", "'version' must be a string"),
        ("type: cog\nname: no\ndescription: d", "'name' must be a string"),
        ("type: cog\nname: a\ndescription: 0x10", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: .inf", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: 2001-12-14T02:59:43.1Z", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: ~", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: 0o17", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: 0b101", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: 1_000", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: 190:20:30.15", "'description' must be a string"),
        ("type: cog\nname: a\ndescription: d\nkind: 1", "'kind' must be a string"),
        ("type: cog\nname: a\ndescription: d\nlicense: 2001-01-01", "'license' must be a string"),
        ("type: cog\nname: a\ndescription: d\nmanifest: 1\nmanifest_schema: x [0.1]", "'manifest' must be a string"),
        ("type: cog\nname: a\ndescription: d\nmanifest: x\nmanifest_schema: 1", "'manifest_schema' must be a string"),
    ],
)
def test_unquoted_non_string_where_a_string_is_required(frontmatter: str, expected: str):
    doc = read_cog_document(cog_md(frontmatter))

    assert expected in doc.errors, doc.errors
    assert doc.errors.count(expected) == 1


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ('type: cog\nname: a\ndescription: d\nversion: "1.0"', []),
        ("type: cog\nname: a\ndescription: d\nversion: 1.0.0", []),
        ("type: cog\nname: a\ndescription: d\nversion: '12:30'", []),
        ("type: cog\nname: a\ndescription: 0X_", []),  # uppercase radix prefix is just a string
        ("type: cog\nname: a\ndescription: 2001-13-45-x", []),  # not timestamp-shaped
    ],
)
def test_quoted_or_unambiguous_values_are_strings(frontmatter: str, expected: list[str]):
    assert read_cog_document(cog_md(frontmatter)).errors == expected


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("type: cog\n\tname: a", "frontmatter line 2 contains a tab character"),
        ("type: cog\n  name: a", "unexpected indentation at frontmatter line 2"),
        ("type: cog\njust text", "cannot parse frontmatter line 2"),
        ('type: cog\n"quoted key": a', "field key at frontmatter line 2 contains whitespace or is quoted"),
        ("type: cog\n&anchor: a", "key '&anchor' begins with a YAML indicator character"),
        ("type: cog\n2001-99-99: a", "key '2001-99-99' resembles a YAML timestamp but names no real date or time"),
        ("type: cog\ntype: cog", "duplicate key: type"),
        ("type: cog\nnested:\n  a: b", "nested mapping under 'nested' is outside the supported subset"),
        ("type: cog\ndescription: &a x", "anchors, aliases, tags, and block scalars are outside the supported subset"),
        ("type: cog\ndescription: *a", "anchors, aliases, tags, and block scalars are outside the supported subset"),
        (
            "type: cog\ndescription: !!str x",
            "anchors, aliases, tags, and block scalars are outside the supported subset",
        ),
        (
            "type: cog\ndescription: |\n  block",
            "anchors, aliases, tags, and block scalars are outside the supported subset",
        ),
        ("type: cog\ndescription: {a: b}", "flow mappings are outside the supported subset"),
        ("type: cog\ndescription: - x", "'-' followed by whitespace is a YAML indicator, not a value"),
        ("type: cog\ndescription: ends:", "a plain scalar may not end with ':'; quote the value"),
        ("type: cog\ndescription: 0x_", "a radix prefix with no digits is not a number any loader can build"),
        ("type: cog\ndescription: 9999-99-99", "resembles a YAML timestamp but names no real date or time"),
        ("type: cog\ndescription: 2001-01-01 25:00:00", "resembles a YAML timestamp but names no real date or time"),
        ("type: cog\ndescription: 2001-01-01 20:00:00+24:00", "resembles a YAML timestamp"),
        ("type: cog\ndescription: 2001-01-01 20:00:00+02:99", "resembles a YAML timestamp"),
        ('type: cog\ndescription: "unterminated', "unterminated quoted string"),
        ("type: cog\ndescription: 'unterminated", "unterminated quoted string"),
        ('type: cog\ndescription: "a" b', "unterminated double-quoted string"),
        ("type: cog\ndescription: 'a' b", "unterminated single-quoted string"),
        ("type: cog\ndescription: 'it's'", "unterminated quoted string"),
        ("type: cog\ndescription: 'a'b'c'", "malformed single-quoted string (single quotes must be doubled)"),
        ('type: cog\ndescription: "a"b"c"', "unterminated double-quoted string"),
        ('type: cog\ndescription: "bad \\q escape"', "unsupported escape '\\\\q' in double-quoted string"),
        ('type: cog\ndescription: "bad \\x4"', "malformed \\x escape in double-quoted string"),
        ('type: cog\ndescription: "bad \\uD800"', "\\u escape is not a Unicode scalar value"),
        ('type: cog\ndescription: "trailing \\', "unterminated quoted string"),
        ('type: cog\ndescription: "a" "b"', "unterminated double-quoted string"),
        ("type: cog\ndescription: [a, b", "unterminated flow collection"),
        ("type: cog\ndescription: [a, b] c", "unexpected text after the end of the flow sequence"),
        ("type: cog\ndescription: [a, [b]]", "lists of collections are outside the supported subset"),
        ("type: cog\ndescription: [a, {b: c}]", "lists of collections are outside the supported subset"),
        ("type: cog\ndescription: a]", None),  # brackets are ordinary characters inside a plain scalar
        ("type: cog\ndescription: ]a", "a plain scalar may not begin with ']'; quote the value"),
        ('type: cog\ndescription: "a"]', "']' closes a flow collection that was never opened"),
        ('type: cog\ndescription: [a"]"]', "']' closes a flow collection that was never opened"),
        ("type: cog\ndescription: [a}", "mismatched flow collection delimiters"),
        ("type: cog\ndescription: [a: b]", "lists of mappings are outside the supported subset"),
        ("type: cog\ndescription: [a,,b]", "empty item in list"),
        ("type: cog\ndescription: [a?b]", "'?' in a plain flow-sequence item; quote the item"),
        ("type: cog\ndescription: [:a]", "a plain flow-sequence item may not begin with ':'"),
        ("type: cog\ndescription: [&a]", "list item: anchors, aliases, tags, and block scalars"),
        ('type: cog\ndescription: ["a]', "unterminated quoted string"),
        ("type: cog\ndescription: [a, 'b]", "unterminated quoted string"),
        ("type: cog\ndescription: [ [a] ]", "lists of collections are outside the supported subset"),
        ("type: cog\ndescription: ['a', \"b\\\"c\", 'd''e']", None),  # valid: quoted items with escapes
        ("type: cog\nlist:\n  - a\n   - b", "inconsistent list indentation (nested lists are unsupported)"),
        ("type: cog\nlist:\n  - [a]", "lists of collections are outside the supported subset"),
        ("type: cog\nlist:\n  - a: b", "lists of mappings are outside the supported subset"),
        ('type: cog\nlist:\n  - "a', "list item: unterminated quoted string"),
        ("type: cog\nlist:\n  - &a", "list item: anchors, aliases, tags, and block scalars"),
        ("type: cog\nlist:\n  - # nothing", "empty item in list"),
        ("type: cog\nmetadata:\n  - a", None),  # a list named metadata parses; the field check complains
        ("type: cog\nmetadata:\n  a b: c", "metadata entries must be single-line 'key: value' pairs"),
        ("type: cog\nmetadata:\n  a: b\n   c: d", "inconsistent metadata indentation"),
        ("type: cog\nmetadata:\n  &a: b", "metadata key '&a' begins with a YAML indicator character"),
        ("type: cog\nmetadata:\n  1: b", "metadata key '1' is not a string"),
        ("type: cog\nmetadata:\n  a: b\n  a: c", "duplicate metadata key: a"),
        ('type: cog\nmetadata:\n  a: "b', "metadata value for 'a': unterminated quoted string"),
        ("type: cog\nmetadata:\n  a:", "metadata value for 'a' is missing"),
        ("type: cog\nmetadata:\n  a: [x]", "metadata value for 'a': flow sequences are supported only as a top-level"),
        ("type: cog\nmetadata:\n  a: {x}", "metadata value for 'a': flow mappings are outside the supported subset"),
    ],
)
def test_frontmatter_outside_the_subset_is_refused(frontmatter: str, expected: str | None):
    doc = read_cog_document(cog_md(frontmatter))

    if expected is None:
        assert not any("outside the supported YAML subset" in error for error in doc.errors), doc.errors
    else:
        assert len(doc.errors) == 1, doc.errors
        assert expected in doc.errors[0]
        assert doc.fields == {}
        assert doc.raw == frontmatter  # the block is still kept for the record


def test_metadata_that_is_not_a_mapping_is_a_field_error():
    assert "'metadata' must be a mapping of string keys to string values" in (
        read_cog_document(cog_md(f"{MINIMAL}\nmetadata: flat")).errors
    )
    assert "'metadata' must be a mapping of string keys to string values" in (
        read_cog_document(cog_md(f"{MINIMAL}\nmetadata:\n  - a")).errors
    )


def test_required_field_bounds():
    assert read_cog_document(cog_md('type: cog\nname: a\ndescription: ""')).errors == [
        "'description' must be non-empty"
    ]
    long_description = "x" * 1025
    assert read_cog_document(cog_md(f"type: cog\nname: a\ndescription: {long_description}")).errors == [
        "'description' must be no more than 1024 characters"
    ]
    long_name = "a" * 65
    assert (
        read_cog_document(cog_md(f"type: cog\nname: {long_name}\ndescription: d"))
        .errors[0]
        .startswith(f"'name' value '{long_name}' violates")
    )
    assert read_cog_document(cog_md("type: cog\nname:\ndescription: d")).errors == [
        "frontmatter is missing the required 'name' field"
    ]


def test_missing_all_required_fields_reports_each():
    doc = read_cog_document(cog_md("kind: context"))

    assert doc.errors == [
        "frontmatter is missing the required 'type' field",
        "frontmatter is missing the required 'name' field",
        "frontmatter is missing the required 'description' field",
    ]


@pytest.mark.parametrize(
    ("data", "expected", "body"),
    [
        (b"---\n---\nbody\n", "frontmatter block is empty", "body\n"),
        (b"---\n   \n---\nbody\n", "frontmatter block is empty", "body\n"),
        (b"---\ntype: cog\nno end\n", "unterminated frontmatter block", "---\ntype: cog\nno end\n"),
        (b"just markdown\n", "file does not begin with YAML frontmatter", "just markdown\n"),
        (b"", "file does not begin with YAML frontmatter", ""),
    ],
)
def test_document_splitting_errors_keep_the_body(data: bytes, expected: str, body: str):
    doc = read_cog_document(data)

    assert doc.errors == [expected]
    assert doc.fields == {}
    assert doc.body == body


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("/etc/cog.yaml", "'manifest' uses an absolute path: /etc/cog.yaml"),
        ("C:\\cogs\\cog.yaml", "'manifest' uses an absolute path: C:\\cogs\\cog.yaml"),
        ("file:///tmp/cog.yaml", "'manifest' uses an absolute path: file:///tmp/cog.yaml"),
        ("https://example.org/cog.yaml", "'manifest' is not a bundled file path: https://example.org/cog.yaml"),
        ("..\\cog.yaml", "'manifest' escapes the bundle root: ..\\cog.yaml"),
        ("a/../../cog.yaml", "'manifest' escapes the bundle root: a/../../cog.yaml"),
        ("..", "'manifest' escapes the bundle root: .."),
        ('""', "'manifest' must not be empty"),
        ('"  "', "'manifest' must not be empty"),
    ],
)
def test_manifest_pointer_must_stay_inside_the_bundle(manifest: str, expected: str):
    card = read_cog_bundle({"COG.md": cog_md(f"{MINIMAL}\nmanifest: {manifest}\nmanifest_schema: x [0.1]")})

    assert card.errors == [expected]
    assert card.profile_status == "missing"
    assert card.manifest is None


def test_manifest_pointer_is_normalized_before_lookup():
    files = {
        "COG.md": cog_md(
            f"{MINIMAL}\nmanifest: ./manifests/../cog.yaml\nmanifest_schema: openteams/cog-manifest [0.1]"
        ),
        "cog.yaml": b"id: example/sample-worker\nkind: context\n",
    }

    card = read_cog_bundle(files)

    assert card.profile_status == "parsed"
    assert card.id == "example/sample-worker"


def test_empty_manifest_schema_is_reported():
    card = read_cog_bundle(
        {"COG.md": cog_md(f'{MINIMAL}\nmanifest: cog.yaml\nmanifest_schema: ""'), "cog.yaml": b"id: x\n"}
    )

    assert card.errors == ["'manifest_schema' must not be empty"]
    assert card.profile_status == "unparsed"


def test_named_manifest_without_schema_is_unparsed_not_guessed():
    card = read_cog_bundle(
        {"COG.md": cog_md(f"{MINIMAL}\nmanifest: pixi.toml"), "pixi.toml": b"[tool.cog]\nid='x/y'\n"}
    )

    assert card.profile_status == "unparsed"
    assert card.profile is None
    assert card.profile_raw == "[tool.cog]\nid='x/y'\n"
    assert card.errors == ["Cog is missing the required 'manifest_schema' field"]


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

PIXI_COG = f'{MINIMAL}\nversion: "0.1.0"\nmanifest: pixi.toml\nmanifest_schema: openteams/cog-manifest [0.1]'
YAML_COG = f'{MINIMAL}\nversion: "0.1.0"\nmanifest: cog.yaml\nmanifest_schema: openteams/cog-manifest [0.1]'


def test_pixi_profile_without_tool_cog_table_is_unparsed():
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": b"[workspace]\nname = 'x'\n"})

    assert card.profile_status == "unparsed"
    assert card.errors == ["manifest has no [tool.cog] table"]
    assert card.profile_raw == "[workspace]\nname = 'x'\n"


def test_pixi_profile_that_is_not_toml_is_unparsed():
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": b"[tool.cog\nid = "})

    assert card.profile_status == "unparsed"
    assert card.errors[0].startswith("manifest is not valid TOML: ")


def test_manifest_that_is_not_utf8_is_unparsed():
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": b"\xff\xfe"})

    assert card.profile_status == "unparsed"
    assert card.errors == ["pixi.toml is not valid UTF-8"]


def test_known_schema_in_an_unsupported_file_format_is_unparsed():
    front = f"{MINIMAL}\nmanifest: cog.json\nmanifest_schema: openteams/cog-manifest [0.1]"
    card = read_cog_bundle({"COG.md": cog_md(front), "cog.json": b"{}"})

    assert card.profile_status == "unparsed"
    assert card.errors == ["manifest 'cog.json' is neither TOML nor YAML; the 0.1 profile lives in one of those"]


def test_pixi_profile_prefers_its_own_version_and_summary_over_the_workspace():
    pixi = b"""
[project]
version = "9.9.9"
description = "workspace description"

[tool.cog]
id = "example/sample-worker"
version = "0.1.0"
summary = "profile summary"
kind = "context"
"""
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": pixi})

    assert card.errors == []
    assert card.version == "0.1.0"
    assert card.summary == "profile summary"
    assert card.profile_schema is None  # the profile declared no schema; no disagreement to report
    assert card.warnings == []


def test_pixi_profile_falls_back_to_legacy_project_table():
    pixi = b"""
[project]
version = "0.1.0"
description = "from project"

[tool.cog]
id = "example/sample-worker"
kind = "context"
"""
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": pixi})

    assert card.errors == []
    assert (card.version, card.summary) == ("0.1.0", "from project")


def test_pixi_profile_ignores_a_workspace_that_is_not_a_table():
    pixi = b'workspace = "oops"\n[tool.cog]\nid = "example/sample-worker"\n'
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": pixi})

    assert card.version is None
    assert card.errors == []


def test_pixi_tasks_that_are_not_a_table_are_ignored():
    pixi = b'tasks = "oops"\n[tool.cog]\nid = "example/sample-worker"\nversion = "0.1.0"\n'
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": pixi})

    assert card.ops == {"usage": [], "lifecycle": []}


@pytest.mark.parametrize(
    ("cog_yaml", "expected"),
    [
        (b"- a\n- b\n", "manifest must be a YAML mapping"),
        (b"", "manifest must be a YAML mapping"),
        (b"just a string\n", "manifest must be a YAML mapping"),
        (b"a: [\n", "manifest is not valid YAML: "),
        (b"a: !!python/object:os.system x\n", "manifest is not valid YAML: "),
    ],
)
def test_yaml_profile_that_is_not_a_mapping_is_unparsed(cog_yaml: bytes, expected: str):
    card = read_cog_bundle({"COG.md": cog_md(YAML_COG), "cog.yaml": cog_yaml})

    assert card.profile_status == "unparsed"
    assert len(card.errors) == 1 and card.errors[0].startswith(expected), card.errors


def test_yaml_profile_reads_tasks_from_a_sibling_pixi_toml_when_it_can():
    files = {
        "COG.md": cog_md(YAML_COG),
        "cog.yaml": b"id: example/sample-worker\nversion: '0.1.0'\nkind: context\n",
        "pixi.toml": b"[tasks]\nask = 'x'\ntest = 'y'\n",
    }
    card = read_cog_bundle(files)
    assert card.ops == {"usage": ["ask"], "lifecycle": ["test"]}
    assert card.audience_inferred is True

    files["pixi.toml"] = b"[tasks\n"
    card = read_cog_bundle(files)
    assert card.profile_status == "parsed"
    assert card.ops == {"usage": [], "lifecycle": []}
    assert len(card.warnings) == 1 and card.warnings[0].startswith(
        "pixi.toml tasks not read: pixi.toml is not valid TOML"
    )

    del files["pixi.toml"]
    card = read_cog_bundle(files)
    assert card.ops == {"usage": [], "lifecycle": []} and card.warnings == []


def test_yaml_profile_values_are_reduced_to_json():
    cog_yaml = b"""
id: example/sample-worker
version: "0.1.0"
kind: context
released: 2001-12-14
at: 2001-12-14t21:59:43.10-05:00
blob: !!binary "aGVsbG8="
tags: !!set {a: null}
1: numeric key
ratio: .nan
"""
    card = read_cog_bundle({"COG.md": cog_md(YAML_COG), "cog.yaml": cog_yaml})

    assert card.profile_status == "parsed", card.errors
    assert card.profile["released"] == "2001-12-14"
    assert card.profile["at"].startswith("2001-12-14T21:59:43.100000-05:00")
    assert card.profile["blob"] == "hello"
    assert card.profile["tags"] == ["a"]
    assert card.profile["1"] == "numeric key"
    assert card.profile["ratio"] == "nan"
    json.dumps(card.to_dict())


def test_jsonable_folds_every_type_json_has_no_spelling_for():
    import datetime

    assert jsonable({1: (1, 2), "t": datetime.time(1, 2)}) == {"1": [1, 2], "t": "01:02:00"}
    assert jsonable(float("inf")) == "inf"
    assert jsonable(frozenset({"a"})) == ["a"]
    assert jsonable(b"\xff") == "\ufffd"
    assert jsonable(object).startswith("<class")
    assert jsonable(True) is True and jsonable(None) is None


def test_schema_alias_in_both_places_raises_no_warning():
    front = f"{MINIMAL}\nmanifest: cog.yaml\nmanifest_schema: openteams/cog-package [0.1]"
    card = read_cog_bundle(
        {"COG.md": cog_md(front), "cog.yaml": b"schema: openteams/cog-package [0.1]\nid: example/sample-worker\n"}
    )

    assert card.profile_status == "parsed"
    assert card.warnings == []
    assert card.profile_schema == "openteams/cog-package [0.1]"


def test_unknown_profile_schema_declared_inside_the_profile_is_a_plain_disagreement():
    card = read_cog_bundle(
        {"COG.md": cog_md(YAML_COG), "cog.yaml": b"schema: example.org/other [3.0]\nid: example/sample-worker\n"}
    )

    assert card.warnings == [
        "frontmatter manifest_schema 'openteams/cog-manifest [0.1]' and profile schema "
        "'example.org/other [3.0]' disagree"
    ]


# ---------------------------------------------------------------------------
# Card derivation: the build tool's rules, and what happens off the happy path
# ---------------------------------------------------------------------------


def test_lifecycle_task_set_is_the_build_tools():
    assert LIFECYCLE_TASKS == {"resolve", "use", "check", "eval", "test", "bundle", "serve"}


def test_declared_audience_beats_the_lifecycle_name_fallback():
    cog_yaml = b"""
id: example/sample-worker
version: "0.1.0"
kind: context
interfaces:
  - {name: a, kind: command, task: check, audience: usage}
  - {name: b, kind: command, task: deploy, audience: lifecycle}
  - {name: c, kind: command}
  - {name: d, kind: command, task: 7}
  - {name: e, kind: command, task: ""}
"""
    card = read_cog_bundle({"COG.md": cog_md(YAML_COG), "cog.yaml": cog_yaml})

    assert card.ops == {"usage": ["check"], "lifecycle": ["deploy"]}
    assert card.audience_inferred is False
    assert len(card.entry_points) == 5
    assert card.warnings == []


def test_malformed_profile_collections_are_skipped_with_warnings():
    cog_yaml = b"""
id: example/sample-worker
version: "0.1.0"
kind: model
summary:
interfaces: "not a list"
requires:
  - capability: a/b
    satisfied_by: "not a mapping"
    also_satisfied_by: {cog: ignored}
  - just a string
context: 3
model: [not, a, mapping]
evaluation: "x"
provides: "one"
prohibits:
"""
    card = read_cog_bundle({"COG.md": cog_md(YAML_COG), "cog.yaml": cog_yaml})

    assert card.profile_status == "parsed"
    assert card.summary == ""
    assert card.entry_points == [] and card.ops == {"usage": [], "lifecycle": []}
    assert card.requires == [{"capability": "a/b", "locality": "any", "satisfiers": []}]
    assert card.model == {
        "name": None,
        "quantization": None,
        "runtime": None,
        "revision": None,
        "served_model_id": None,
        "address": None,
    }
    assert card.provides == "one" and card.prohibits == []
    assert card.input_contract is None and card.fixtures == []
    assert card.warnings == [
        "profile 'interfaces' is not a list; ignored",
        "profile 'requires' has 1 entr(y/ies) that are not mappings; skipped",
        "profile 'context' is not a mapping; ignored",
        "profile 'model' is not a mapping; ignored",
        "profile 'evaluation' is not a mapping; ignored",
    ]


def test_model_block_takes_address_over_endpoint_from_the_default_interface():
    cog_yaml = b"""
id: example/sample-worker
version: "0.1.0"
kind: model
model: {name: m, quantization: q, runtime: r, revision: v}
interfaces:
  - {name: other, kind: openai-compatible, task: serve, endpoint: http://x/other}
  - name: main
    kind: openai-compatible
    task: serve
    endpoint: http://x/v1
    address: host:8080
    served_model_id: m1
    default: true
"""
    card = read_cog_bundle({"COG.md": cog_md(YAML_COG), "cog.yaml": cog_yaml})

    assert card.model == {
        "name": "m",
        "quantization": "q",
        "runtime": "r",
        "revision": "v",
        "served_model_id": "m1",
        "address": "host:8080",
    }
    assert card.ops == {"usage": [], "lifecycle": ["serve"]}


def test_envelope_falls_back_to_the_fetched_files_when_no_path_list_is_given():
    files = {"COG.md": cog_md(PIXI_COG), "pixi.toml": b'[tool.cog]\nid = "example/sample-worker"\n'}

    assert read_cog_bundle(files).envelope is None
    assert read_cog_bundle({**files, "src/cog_core.py": b""}).envelope == 1
    assert read_cog_bundle(files, bundle_paths=["src/cog_core.py"]).envelope == 1
    assert read_cog_bundle({**files, "src/cog_core.py": b""}, bundle_paths=[]).envelope is None


# ---------------------------------------------------------------------------
# Agreement between frontmatter and profile
# ---------------------------------------------------------------------------


def test_name_conflict_is_an_error():
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": b'[tool.cog]\nid = "example/other-worker"\n'})

    assert card.errors == [
        "conflict: profile id 'example/other-worker' does not end with frontmatter name 'sample-worker'"
    ]
    assert card.name == "sample-worker" and card.id == "example/other-worker"


def test_bare_profile_id_is_compared_whole():
    card = read_cog_bundle({"COG.md": cog_md(PIXI_COG), "pixi.toml": b'[tool.cog]\nid = "sample-worker"\n'})

    assert card.errors == []


def test_kind_disagreement_is_a_warning_not_an_error():
    front = f"{PIXI_COG}\nkind: context"
    pixi = b'[tool.cog]\nid = "example/sample-worker"\nversion = "0.1.0"\nkind = "complete"\n'

    card = read_cog_bundle({"COG.md": cog_md(front), "pixi.toml": pixi})

    assert card.errors == []
    assert card.warnings == ["profile kind 'complete' disagrees with frontmatter kind 'context'"]
    assert card.kind == "complete" and card.frontmatter["kind"] == "context"


def test_agreement_is_only_checked_when_both_sides_speak():
    # Frontmatter version absent: nothing to compare. Profile id missing: same.
    front = f"{MINIMAL}\nmanifest: pixi.toml\nmanifest_schema: openteams/cog-manifest [0.1]"
    card = read_cog_bundle({"COG.md": cog_md(front), "pixi.toml": b'[tool.cog]\nversion = "3.0.0"\n'})

    assert card.errors == []
    assert card.version == "3.0.0"


def test_non_string_frontmatter_version_still_conflicts_by_text():
    front = f"{MINIMAL}\nversion: 0.1\nmanifest: pixi.toml\nmanifest_schema: openteams/cog-manifest [0.1]"
    pixi = b'[tool.cog]\nid = "example/sample-worker"\nversion = "0.1.0"\n'

    card = read_cog_bundle({"COG.md": cog_md(front), "pixi.toml": pixi})

    assert card.errors == [
        "'version' must be a string",
        "conflict: profile version '0.1.0' disagrees with frontmatter version '0.1'",
    ]


# ---------------------------------------------------------------------------
# Bundles without COG.md
# ---------------------------------------------------------------------------


def test_bundle_without_cog_md_or_pixi_toml_is_not_a_cog():
    card = read_cog_bundle({"README.md": b"hi"})

    assert card.errors == ["bundle has no COG.md"]
    assert card.profile_status == "missing" and card.kind is None


def test_pixi_only_bundle_without_capability_is_not_a_cog():
    card = read_cog_bundle({"pixi.toml": b"[workspace]\nname = 'x'\n"})

    assert card.errors == ["bundle has no COG.md and pixi.toml declares no [tool.nebi.capability]"]
    assert card.kind is None and card.profile is None


def test_pixi_only_bundle_that_is_not_toml_reports_both_problems():
    card = read_cog_bundle({"pixi.toml": b"[["})

    assert card.errors[0] == "bundle has no COG.md and pixi.toml declares no [tool.nebi.capability]"
    assert card.errors[1].startswith("pixi.toml is not valid TOML: ")


def test_pixi_only_bundle_that_is_not_utf8_reports_both_problems():
    card = read_cog_bundle({"pixi.toml": b"\xff"})

    assert card.errors == [
        "bundle has no COG.md and pixi.toml declares no [tool.nebi.capability]",
        "pixi.toml is not valid UTF-8",
    ]


def test_capability_table_without_entries_is_unparsed():
    pixi = b'[tool.nebi.capability]\nspec-version = "0.2.0"\nexample = "not a table"\n'
    card = read_cog_bundle({"pixi.toml": pixi})

    assert card.kind == "prog"
    assert card.profile_status == "unparsed"
    assert card.errors == ["[tool.nebi.capability] declares no <org>.<key> capability table"]
    assert card.warnings == ["[tool.nebi.capability] spec-version '0.2.0' is not '0.1.0'"]
    assert card.profile == {"spec-version": "0.2.0", "example": "not a table"}


def test_multiple_capabilities_describe_the_first_and_keep_the_rest():
    pixi = b"""
project = "not a table"
[tool.nebi.capability]
spec-version = "0.1.0"
[tool.nebi.capability.example.one]
description = "first"
author = "just a string"
default-target = "local"
targets = "not a table"
[tool.nebi.capability.example.two]
description = "second"
[tool.nebi.capability.example.two.targets.local]
task = 3
[tool.nebi.capability.example.two.targets.other]
task = "x"
"""
    card = read_cog_bundle({"pixi.toml": pixi})

    assert card.profile_status == "parsed"
    assert card.id == "example/one" and card.summary == "first"
    assert card.publisher is None and card.version is None
    assert card.entry_points == [] and card.ops == {"usage": [], "lifecycle": []}
    assert card.warnings == [
        "pixi.toml declares 2 capabilities; the card describes the first (example/two kept in profile)"
    ]
    assert card.profile["example"]["two"]["targets"]["local"]["task"] == 3


def test_prog_targets_that_are_not_tables_or_lack_a_task_are_tolerated():
    pixi = b"""
[tool.nebi.capability]
spec-version = "0.1.0"
[tool.nebi.capability.example.one]
description = "d"
default-target = "b"
[tool.nebi.capability.example.one.targets]
a = "not a table"
[tool.nebi.capability.example.one.targets.b]
environment = "default"
"""
    card = read_cog_bundle({"pixi.toml": pixi})

    assert card.entry_points == [
        {"name": "b", "kind": "command", "task": None, "audience": "usage", "endpoint": None, "default": True}
    ]
    assert card.ops == {"usage": [], "lifecycle": []}


def test_cog_md_wins_over_a_capability_table():
    card = read_cog_bundle(
        {"COG.md": cog_md(MINIMAL), "pixi.toml": b"[tool.nebi.capability.example.one]\ndescription = 'd'\n"}
    )

    assert card.kind is None and card.profile_status == "draft"


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------


def test_reader_guard_turns_an_unexpected_exception_into_a_card(monkeypatch):
    def explode(files, bundle_paths):
        raise KeyError("unexpected")

    monkeypatch.setattr(bundle_module, "_read", explode)

    card = read_cog_bundle({"COG.md": b""})

    assert card.errors == ["reader error: KeyError: 'unexpected'"]
    assert card.profile_status == "missing"


def test_reader_never_raises_on_random_bytes_and_stays_serialisable():
    rng = random.Random(81)
    valid, _ = load_fixture("pixi-context")
    seeds = [valid["COG.md"], valid["pixi.toml"], (FIXTURES / "yaml-model" / "cog.yaml").read_bytes()]
    alphabet = b"-:?[]{}#&*!|>'\"%@`,\\ \t\n0123456789abcxyz.e_~ATZ/"
    for case in range(400):
        files: dict[str, bytes] = {}
        for name in ("COG.md", "pixi.toml", "cog.yaml"):
            if rng.random() < 0.15:
                continue  # sometimes the file is absent
            mode = rng.random()
            if mode < 0.3:
                data = rng.randbytes(rng.randrange(0, 200))
            elif mode < 0.6:
                data = bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 300)))
            else:
                # Mutate a valid document: splice indicator characters into it.
                base = bytearray(rng.choice(seeds))
                for _ in range(rng.randrange(1, 12)):
                    position = rng.randrange(0, len(base) + 1)
                    base[position:position] = bytes([rng.choice(alphabet)])
                data = bytes(base)
            files[name] = data
        paths = None if rng.random() < 0.5 else list(files) + (["src/cog_core.py"] if rng.random() < 0.5 else [])

        card = read_cog_bundle(files, bundle_paths=paths)

        as_dict = card.to_dict()
        assert list(as_dict) == BUILD_TOOL_KEYS + HUB_KEYS, case
        json.dumps(as_dict)
        assert card.profile_status in {"parsed", "draft", "unparsed", "missing"}, case
        assert card.profile_status != "parsed" or card.profile is not None, case


@pytest.mark.parametrize(
    "data",
    [
        b"---\n",
        b"---\n---\n",
        b"---\n\n---",
        b"---\n---\n---\n",
        b"---\ntype: cog\nname: a\ndescription: d\n---",
        b"---\n:\n---\n",
        b"---\n: x\n---\n",
        b"---\n-: x\n---\n",
        b"---\n- x: v\n---\n",
        b"---\nx: '\n---\n",
        b'---\nx: "\n---\n',
        b"---\nx: [\n---\n",
        b"---\nx: ]\n---\n",
        b"---\nx: {\n---\n",
        b"---\nx: [a,\n---\n",
        b"---\nx: [']\n---\n",
        b'---\nx: ["\\\n---\n',
        b'---\nx: "\\"\n---\n',
        b"---\nx: 0x\n---\n",
        b"---\nx: 2001-\n---\n",
        b"---\nx: 2001-02-30\n---\n",
        b"---\nx:\n  -\n---\n",
        b"---\nx:\n  - \n---\n",
        b"---\nmetadata:\n  \n  a: b\n---\n",
        b"---\nmetadata:\n  a: b\n  c\n---\n",
        b"---\nx: a #\n---\n",
        b"---\nx: #\n---\n",
        b"---\nx: '''\n---\n",
        b"---\nx: [a, 'b''c', \"d\\\"e\"]\n---\n",
        b"---\nx: [a\\,b]\n---\n",
        b"---\n\xf0\x9f\x98\x80: \xf0\x9f\x98\x80\n---\n",
        b"\xef\xbb\xbf",
        b"\xef\xbb\xbf---",
        b"\r\n---\r\n",
        b"---\r---\r",
    ],
)
def test_adversarial_documents_never_raise(data: bytes):
    card = read_cog_bundle({"COG.md": data}, bundle_paths=["COG.md"])

    json.dumps(card.to_dict())
    assert card.profile_status in {"draft", "missing"}
