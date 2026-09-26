#!/usr/bin/env python3
"""Pin the release version into the files that carry it.

Called by .github/workflows/semantic-release.yml with the computed version.
Updates helm/collab-hub/Chart.yaml (version + appVersion) and
api/pyproject.toml (project version) for the release commit, which is pushed
only as the collab-hub-<version> tag. build-images.yaml and release.yaml run
from that tag, so the chart, image, and tag all describe the same commit.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sub(path: Path, pattern: str, replacement: str, count: int) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE)
    if n != count:
        sys.exit(f"{path}: expected {count} substitution(s) for {pattern!r}, made {n}")
    path.write_text(new)


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", sys.argv[1]):
        sys.exit(f"usage: {sys.argv[0]} <semver-version>")
    version = sys.argv[1]

    chart = ROOT / "helm" / "collab-hub" / "Chart.yaml"
    sub(chart, r'^version: ".*"$', f'version: "{version}"', 1)
    sub(chart, r'^appVersion: ".*"$', f'appVersion: "{version}"', 1)

    pyproject = ROOT / "api" / "pyproject.toml"
    sub(pyproject, r'^version = ".*"$', f'version = "{version}"', 1)

    print(f"pinned {version} into {chart.relative_to(ROOT)} and {pyproject.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
