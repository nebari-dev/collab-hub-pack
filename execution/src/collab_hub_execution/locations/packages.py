"""The directory package source: a Cog package found by name in a directory, for development.

A ``local`` worker runs a Cog package that sits on the controller's disk. This
source maps an allowlisted name to a directory under a configured root, and
refuses a name or a path that leaves the roots. It is not a registry source:
it resolves no reference and pulls nothing.

A package is a directory with a ``pixi.toml`` that declares a ``serve`` task,
the command that serves the seam (``POST /invoke``, ``GET /healthz``), and the
``pixi.lock`` that pins its environment. Both are files of the package itself:
a symbolic link in their place is refused, so neither leads outside the roots.
It is identified by its name and by the digest of its manifest and its lock, so a
development run is recognisable on the Track and never mistaken for a published
Cog, which is identified by its artifact's digest.

The hub's bundle reader (``collab_hub_api.cogs.bundle``) reads the same files
for the catalog; this package does not import the API, so the source reads the
two things it needs here, the ``serve`` task and the digest, itself.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MANIFEST = "pixi.toml"
LOCK = "pixi.lock"
SERVE_TASK = "serve"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?$")


class PackageRefused(ValueError):
    """A name or a path the source will not resolve: not allowlisted, or outside its roots."""


class PackageNotFound(LookupError):
    """No package of that name under the roots, or one that declares no ``serve`` task."""


@dataclass(frozen=True, slots=True)
class CogPackage:
    """A Cog package on disk, and what a worker of it is started with."""

    name: str
    directory: Path
    serve: str
    """The ``serve`` task's command, as the manifest declares it."""
    digest: str
    """``sha256:`` of the manifest and the lock: the identity of a development package."""

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST


class DirectoryPackageSource:
    """Resolves a Cog's name to a package directory under one of its roots.

    ``allow`` lists the names that may be resolved; ``None`` allows every
    package the roots hold. Symbolic links are followed before the check, so a
    link inside a root cannot lead outside it.
    """

    def __init__(self, roots: Iterable[str | Path], allow: Iterable[str] | None = None) -> None:
        self.roots = tuple(Path(root).resolve() for root in roots)
        if not self.roots:
            raise ValueError("the directory package source needs at least one root")
        self.allow = None if allow is None else frozenset(allow)

    def names(self) -> tuple[str, ...]:
        """The packages the source can resolve, by name."""
        found = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest in sorted(root.glob(f"*/{MANIFEST}")) + sorted(root.glob(f"*/*/{MANIFEST}")):
                name = manifest.parent.relative_to(root).as_posix()
                if (self.allow is None or name in self.allow) and _NAME.match(name):
                    found.add(name)
        return tuple(sorted(found))

    def resolve(self, name: str) -> CogPackage:
        if not isinstance(name, str) or not _NAME.match(name) or ".." in name.split("/"):
            raise PackageRefused(
                f"{name!r} is not a package name: one or two segments of letters, digits, '.', '_', '-'")
        if self.allow is not None and name not in self.allow:
            raise PackageRefused(f"package {name!r} is not allowlisted")
        for root in self.roots:
            directory = (root / name).resolve()
            if not directory.is_relative_to(root):
                # A symbolic link under the root that leads out of it.
                raise PackageRefused(f"package {name!r} resolves outside its root")
            if (directory / MANIFEST).is_file():
                return self._read(name, directory)
        raise PackageNotFound(f"no package {name!r} under {', '.join(str(root) for root in self.roots)}")

    @staticmethod
    def _own_file(name: str, directory: Path, file: str) -> bytes:
        """A file of the package, read without following a link: the bytes are the package's own."""
        try:
            descriptor = os.open(directory / file, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise PackageRefused(
                f"package {name!r} has no {file}: a runnable package carries its manifest and its lock") from None
        except OSError as exc:  # ELOOP: the file is a symbolic link
            raise PackageRefused(f"package {name!r} has a {file} that is a symbolic link or cannot be read: "
                                 f"{exc.strerror}") from exc
        with os.fdopen(descriptor, "rb") as handle:
            if not os.path.isfile(handle.fileno()):
                raise PackageRefused(f"package {name!r} has a {file} that is not a file")
            return handle.read()

    @classmethod
    def _read(cls, name: str, directory: Path) -> CogPackage:
        manifest = cls._own_file(name, directory, MANIFEST)
        # The lock is part of what the package is: without it the environment that runs would be
        # resolved at launch, and the digest would not name it.
        lock = cls._own_file(name, directory, LOCK)
        try:
            document = tomllib.loads(manifest.decode())
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise PackageNotFound(f"package {name!r} has a {MANIFEST} that is not TOML: {exc}") from exc
        task = document.get("tasks", {}).get(SERVE_TASK)
        command = task.get("cmd") if isinstance(task, dict) else task
        if not isinstance(command, str) or not command.strip():
            raise PackageNotFound(f"package {name!r} declares no `{SERVE_TASK}` task in its {MANIFEST}")
        digest = hashlib.sha256()
        digest.update(manifest)
        digest.update(b"\0")
        digest.update(lock)
        return CogPackage(name=name, directory=directory, serve=command.strip(), digest=f"sha256:{digest.hexdigest()}")
