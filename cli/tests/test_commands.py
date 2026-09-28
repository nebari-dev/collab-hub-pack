"""The catalog commands, profiles, and the import boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from collab_hub_cli import config

from .conftest import HUB


def _cog(n: int, kind: str = "complete") -> dict:
    return {
        "cog_id": f"acme/cog-{n:03d}", "digest": f"sha256:{n:064x}", "version": f"1.{n}.0",
        "reference": f"registry.test/acme/cog-{n:03d}@sha256:{n:064x}", "repository": f"acme/cog-{n:03d}",
        "tags": ["latest"], "card": {"name": f"cog-{n:03d}", "kind": kind, "description": f"Cog number {n}"},
    }


def test_cog_list_follows_every_page(stub, cli):
    stub.dev_auth = True
    stub.cogs = [_cog(n) for n in range(450)]
    result = cli("--hub", HUB, "cog", "list", "--json")
    assert result.exit_code == 0, result.output
    assert [item["cog_id"] for item in json.loads(result.stdout)] == [c["cog_id"] for c in stub.cogs]
    pages = [r.url.params["offset"] for r in stub.requests if r.url.path == "/v1/cogs"]
    assert pages == ["0", "200", "400"]


def test_cog_list_prints_a_table_and_passes_its_filters(stub, cli):
    stub.dev_auth = True
    stub.cogs = [_cog(1), _cog(2, kind="context")]
    result = cli("--hub", HUB, "cog", "list", "--kind", "context")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["COG", "VERSION", "KIND", "DESCRIPTION"]
    assert lines[1].split()[:3] == ["acme/cog-002", "1.2.0", "context"] and len(lines) == 2
    [request] = [r for r in stub.requests if r.url.path == "/v1/cogs"]
    assert request.url.params["kind"] == "context"


def test_cog_list_passes_every_catalog_filter(stub, cli):
    stub.dev_auth = True
    options = {"--kind": "kind", "--publisher": "publisher", "--provides": "provides", "--requires": "requires",
               "--accepts": "accepts", "--produces": "produces", "--source-id": "source_id", "--query": "q"}
    args = [part for option in options for part in (option, f"value-of-{option}")]
    assert cli("--hub", HUB, "cog", "list", *args).exit_code == 0
    [request] = [r for r in stub.requests if r.url.path == "/v1/cogs"]
    assert {param: request.url.params[param] for param in options.values()} == \
        {param: f"value-of-{option}" for option, param in options.items()}


def test_an_empty_catalog_says_so(stub, cli):
    stub.dev_auth = True
    result = cli("--hub", HUB, "cog", "list")
    assert result.exit_code == 0 and result.stdout == "" and "no Cogs" in result.stderr
    assert json.loads(cli("--hub", HUB, "cog", "list", "--json").stdout) == []


def test_cog_show_prints_the_card_and_a_missing_cog_is_the_hub_s_404(stub, cli):
    stub.dev_auth = True
    stub.cogs = [_cog(7)]
    shown = cli("--hub", HUB, "cog", "show", "acme/cog-007")
    assert shown.exit_code == 0, shown.output
    assert "acme/cog-007" in shown.stdout and "Cog number 7" in shown.stdout
    missing = cli("--hub", HUB, "cog", "show", "acme/nope")
    assert missing.exit_code == 1 and "No Cog acme/nope (HTTP 404)" in missing.stderr


def test_profiles_choose_the_hub_and_flags_and_the_environment_override_them(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    config.save(directory, {"default_profile": "work",
                            "profiles": {"work": {"hub": "https://work.test/"}, "lab": {"hub": "https://lab.test"}}})
    assert config.resolve(None, None, directory).hub == "https://work.test"
    assert config.resolve(None, "lab", directory).hub == "https://lab.test"
    assert config.resolve("https://flag.test", "lab", directory).hub == "https://flag.test"
    assert config.resolve(None, "fresh", directory).hub is None


def test_the_environment_reaches_the_options(stub, cli, monkeypatch):
    stub.dev_auth = True
    monkeypatch.setenv("COLLAB_HUB_URL", HUB)
    assert cli("whoami").exit_code == 0


def test_a_profile_name_cannot_reach_outside_the_credentials_directory(stub, cli):
    result = cli("--hub", HUB, "--profile", "../escape", "whoami")
    assert result.exit_code == 2 and "invalid profile name" in result.stderr


def test_the_cli_imports_no_hub_package():
    # A client, not a second implementation: it talks to the hub over HTTP only.
    source = Path(__file__).resolve().parents[1] / "src" / "collab_hub_cli"
    imported = set()
    for path in source.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert not imported & {"collab_hub_api", "collab_hub_execution"}, imported
