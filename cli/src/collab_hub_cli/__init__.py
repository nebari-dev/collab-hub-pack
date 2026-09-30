"""collab-hub: a command-line client for the Collab Hub.

A client, not a second implementation: every command is an HTTP call to the
hub's REST API, and this package imports nothing from the hub's own packages.
See cli/README.md.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # From the installed metadata, so pyproject.toml is the one place the
    # version is set (scripts/release/bump-version.py pins it per release).
    __version__ = version("collab-hub-cli")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "unknown"
