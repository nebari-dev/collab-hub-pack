"""The signed-in session, one file per profile, readable only by its owner.

``credentials/<profile>.json`` in the configuration directory holds the hub it
was obtained for, the issuer and client that issued it, the tokens and when
they expire. The directory is created ``0700`` and every file is written
``0600`` before a byte of it exists, then moved into place, so a token is
never readable by anyone else, even for a moment.

A session belongs to one hub: it is sent to that hub only, never to another
one a ``--hub`` flag points at.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Credentials:
    hub: str
    access_token: str
    issuer: str | None = None
    client_id: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None
    """Epoch seconds when the access token expires; ``None`` when unknown."""
    obtained_by: str = "browser"
    """``browser`` for the PKCE sign-in, ``token`` for ``login --with-token`` (never renewed)."""


def _path(directory: Path, profile: str) -> Path:
    return directory / "credentials" / f"{profile}.json"


def load(directory: Path, profile: str) -> Credentials | None:
    path = _path(directory, profile)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    try:
        return Credentials(**data)
    except TypeError:
        return None


def save(directory: Path, profile: str, credentials: Credentials) -> Path:
    path = _path(directory, profile)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)  # the mode above is masked by umask; this is not
        os.write(descriptor, json.dumps(asdict(credentials), indent=2).encode())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    return path


def delete(directory: Path, profile: str) -> bool:
    try:
        _path(directory, profile).unlink()
    except FileNotFoundError:
        return False
    return True
