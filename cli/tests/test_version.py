"""The CLI reports the version its own source carries, which matches its project metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

import collab_hub_cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    project = tomllib.loads(PYPROJECT.read_text(encoding="UTF-8"))["project"]
    assert collab_hub_cli.__version__ == project["version"]
