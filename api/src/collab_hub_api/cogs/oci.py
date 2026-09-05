"""Generic OCI Distribution client for indexing Cog bundles.

Everything the indexer needs from a registry that *every* OCI registry speaks:
manifests, digest-verified blobs, tag lists, and the bearer-token challenge
dance from the distribution token spec. Nothing here knows what Harbor, quay,
or GHCR are; vendor-specific concerns (repository enumeration, webhooks,
credential shape) live behind ``cogs.registry.RegistrySource`` adapters.

Public interface (stable for the adapters and the indexer):

- :class:`OCIClient` — one registry, optionally pre-authenticated.
- :class:`Descriptor` / :class:`Manifest` — the parsed content descriptors.
- :func:`select_bundle_layers` / :func:`fetch_bundle_files` — pick and fetch
  exactly the small layers the bundle reader needs.
- The :class:`OCIError` hierarchy — one base so callers can record a failure
  per artifact without knowing which step failed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import httpx

TITLE_ANNOTATION = "org.opencontainers.image.title"

MEDIA_TYPE_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_TYPE_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"

MEDIA_TYPE_PIXI_CONFIG = "application/vnd.pixi.config.v1+toml"
MEDIA_TYPE_PIXI_TOML = "application/vnd.pixi.toml.v1+toml"
MEDIA_TYPE_PIXI_LOCK = "application/vnd.pixi.lock.v1+yaml"
MEDIA_TYPE_NEBI_ASSET = "application/vnd.nebi.asset.v1"

DEFAULT_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_BUNDLE_FILE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

COG_ENTRY_FILE = "COG.md"
DEFAULT_BUNDLE_TITLES: frozenset[str] = frozenset({COG_ENTRY_FILE, "pixi.toml", "cog.yaml"})
"""Layer titles the indexer always wants. The lockfile is never among them."""


class OCIError(Exception):
    """Base for every failure this module reports."""


class OCIAuthError(OCIError):
    """The registry refused the credential (401 after a token was presented, or no usable challenge)."""


class OCINotFound(OCIError):
    """The repository, reference, or blob does not exist (404)."""


class OCIDigestMismatch(OCIError):
    """The bytes received do not hash to the descriptor's digest. Never trust the payload."""


class OCITooLarge(OCIError):
    """A manifest or blob exceeded the configured size cap; the body was not retained."""


class OCIProtocolError(OCIError):
    """Malformed manifest, challenge, token response, or an unexpected status."""


@dataclass(frozen=True)
class BasicCredentials:
    """Username/password presented to the token endpoint (or as HTTP basic auth)."""

    username: str
    password: str


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int
    annotations: Mapping[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        return self.annotations.get(TITLE_ANNOTATION)


@dataclass(frozen=True)
class Manifest:
    media_type: str
    digest: str
    """``Docker-Content-Digest`` when the registry sent one, else sha256 of the body; always verified against the body."""
    config: Descriptor | None
    layers: tuple[Descriptor, ...]
    annotations: Mapping[str, str] = field(default_factory=dict)
    raw: bytes = b""

    def layer_by_title(self, title: str) -> Descriptor | None:
        for layer in self.layers:
            if layer.title == title:
                return layer
        return None


class OCIClient:
    """Minimal OCI Distribution client over ``httpx``.

    ``base_url`` is the registry origin (``https://harbor.example.com`` or an
    in-cluster ``http://harbor-core.harbor.svc``). ``token_url`` pins the
    bearer-token endpoint so an in-cluster caller is not bounced through the
    gateway when the challenge's ``realm`` advertises the external URL; the
    ``service``/``scope`` parameters from the challenge are still honoured.
    ``transport`` exists for tests (``httpx.MockTransport``).
    """

    def __init__(
        self,
        base_url: str,
        *,
        credentials: BasicCredentials | None = None,
        token_url: str | None = None,
        ca_bundle_path: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> OCIClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        raise NotImplementedError

    async def get_manifest(self, repo: str, ref: str) -> Manifest:
        """Fetch a manifest by tag or digest; follow an index to the child carrying pixi layers."""
        raise NotImplementedError

    async def get_blob(self, repo: str, descriptor: Descriptor | str, *, max_bytes: int) -> bytes:
        """Fetch a blob and verify its sha256 against the descriptor digest before returning it."""
        raise NotImplementedError

    async def list_tags(self, repo: str) -> list[str]:
        raise NotImplementedError


def select_bundle_layers(
    manifest: Manifest,
    *,
    manifest_file: str | None = None,
    extra_titles: Iterable[str] = (),
) -> dict[str, Descriptor]:
    """Return the descriptors, keyed by title, the bundle reader needs.

    Always ``COG.md`` and ``pixi.toml`` when present, the profile manifest named
    by the frontmatter (``manifest_file``), and any ``extra_titles``. Never the
    lockfile.
    """
    raise NotImplementedError


async def fetch_bundle_files(
    client: OCIClient,
    repo: str,
    manifest: Manifest,
    *,
    manifest_file: str | None = None,
    extra_titles: Iterable[str] = (),
    max_bytes_per_file: int = DEFAULT_MAX_BUNDLE_FILE_BYTES,
) -> dict[str, bytes]:
    """Fetch the selected layers and return ``{title: bytes}`` for the bundle reader."""
    raise NotImplementedError
