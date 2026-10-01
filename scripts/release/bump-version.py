#!/usr/bin/env python3
"""Pin the release version into the files that carry it.

The version is a Semantic Versioning 2.0.0 version, pre-release suffix allowed:
https://semver.org/spec/v2.0.0.html

Called by .github/workflows/semantic-release.yml with the computed version.
Updates helm/collab-hub/Chart.yaml (version + appVersion),
api/pyproject.toml and cli/pyproject.toml (project version) and the CLI's
__version__ for the release commit, which is pushed only as the
collab-hub-<version> tag. build-images.yaml and release.yaml run
from that tag, so the chart, image, and tag all describe the same commit.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "collab-hub" / "Chart.yaml"
API_PYPROJECT = ROOT / "api" / "pyproject.toml"
CLI_PYPROJECT = ROOT / "cli" / "pyproject.toml"
CLI_INIT = ROOT / "cli" / "src" / "collab_hub_cli" / "__init__.py"
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def sub(path: Path, pattern: str, replacement: str, count: int) -> None:
    text = path.read_text(encoding="UTF-8")
    new, n = re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE)
    if n != count:
        sys.exit(f"{path}: expected {count} substitution(s) for {pattern!r}, made {n}")
    path.write_text(new, encoding="UTF-8")


def semver(value: str) -> str:
    if not SEMVER.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a semantic version such as 1.2.3 or 1.2.3-rc.1")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", type=semver, help="the release version, e.g. 1.2.3")
    version = parser.parse_args().version

    sub(CHART, r'^version: ".*"$', f'version: "{version}"', 1)
    sub(CHART, r'^appVersion: ".*"$', f'appVersion: "{version}"', 1)
    for pyproject in (API_PYPROJECT, CLI_PYPROJECT):
        sub(pyproject, r'^version = ".*"$', f'version = "{version}"', 1)
    sub(CLI_INIT, r'^__version__ = ".*"$', f'__version__ = "{version}"', 1)

    pinned = ", ".join(str(path.relative_to(ROOT)) for path in (CHART, API_PYPROJECT, CLI_PYPROJECT, CLI_INIT))
    print(f"pinned {version} into {pinned}")


if __name__ == "__main__":
    main()
