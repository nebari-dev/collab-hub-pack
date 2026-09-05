"""Registry sources: the seam that keeps the Cog registry swappable.

Almost everything the indexer does is standard OCI and goes through the
generic client in ``cogs.oci``. Three things are not, and they are the only
concerns a vendor adapter may implement:

1. **Repository enumeration** — ``/v2/_catalog`` is optional and registries
   restrict or replace it with their own listing API.
2. **Change notification** — webhook payload shape and event names are
   vendor-defined.
3. **Credential shape** — robot accounts, tokens, app credentials.

:class:`RegistrySource` is the contract the indexer and the webhook receiver
code against. Adapters live under ``cogs.adapters`` and are selected by the
``kind`` of a :class:`CogRegistrySourceConfig`, the way ``frames.org_source``
selects an org source: a mistyped kind or a duplicate id fails in
:func:`build_registry_sources` at startup, not on the first sweep.

Index rows carry the source ``id`` and the reference ``<host>/<repo>@<digest>``
(:func:`reference`). ``host`` always derives from the external ``url`` so an
in-cluster ``api_url`` override never leaks into stored identities.
"""

from __future__ import annotations

import logging
import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from .oci import OCIClient

logger = logging.getLogger(__name__)

RegistryKind = Literal["harbor", "static"]
"""The adapters that exist. The vendor name appears here and in the dispatch of
:func:`build_registry_sources`; nowhere else outside ``cogs/adapters/``."""

# Repository path grammar from the OCI Distribution spec: lowercase components
# separated by "/", each component alphanumerics joined by ".", "_", "__" or
# one or more "-". No leading slash, no empty component, so no "..".
REPOSITORY_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
REPOSITORY_PATTERN = re.compile(rf"^{REPOSITORY_COMPONENT}(?:/{REPOSITORY_COMPONENT})*$")
REPOSITORY_MAX_LENGTH = 255
PROJECT_PATTERN = re.compile(rf"^{REPOSITORY_COMPONENT}$")
"""A project/namespace is exactly one repository component."""

DIGEST_PATTERN = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[a-f0-9]{32,}$")
SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SOURCE_ID_MAX_LENGTH = 64

CREATED_ANNOTATION = "org.opencontainers.image.created"


class RegistrySourceError(Exception):
    """Base for every failure an adapter reports about its own (non-OCI) API.

    OCI failures raised through ``oci()`` keep their ``OCIError`` types; this
    hierarchy covers enumeration and webhook translation.
    """


class RegistrySourceAuthError(RegistrySourceError):
    """The registry's listing API refused the configured credential."""


class RegistrySourceProtocolError(RegistrySourceError):
    """The listing API or an index document answered with an unexpected shape."""


@dataclass(frozen=True)
class ArtifactRef:
    """One artifact as enumerated by a source: identity is the digest."""

    digest: str
    tags: tuple[str, ...] = ()
    pushed_at: datetime | None = None
    media_type: str | None = None


@dataclass(frozen=True)
class RegistryEvent:
    """A vendor webhook payload translated to what the indexer acts on."""

    kind: Literal["push", "delete"]
    repo: str
    digest: str | None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebhookRequest:
    """Transport-neutral view of an inbound webhook so adapters never import the web framework."""

    headers: Mapping[str, str]
    body: bytes


@runtime_checkable
class RegistrySource(Protocol):
    """What the indexer (#84) and the webhook receiver (#86) need from a registry."""

    id: str
    """Stable source id stored with every indexed row."""

    host: str
    """Registry host used in ``<host>/<repo>@<digest>``; derived from the external URL."""

    async def list_repositories(self) -> list[str]:
        """Sorted, de-duplicated OCI repository paths (``project/name``)."""
        ...

    async def list_artifacts(self, repo: str) -> list[ArtifactRef]:
        """One ref per digest, tags grouped and sorted, ordered by digest."""
        ...

    def oci(self) -> OCIClient:
        """The generic client, pre-authenticated; the same instance for the source's lifetime."""
        ...

    def parse_event(self, request: WebhookRequest) -> list[RegistryEvent] | None:
        """Translate a webhook delivery. ``None`` means "not mine"; ``[]`` means "mine, nothing to do"."""
        ...

    async def aclose(self) -> None: ...


def reference(host: str, repo: str, digest: str) -> str:
    """The pinned install reference ``<host>/<repo>@<digest>`` stored with every indexed row."""

    if not host or "/" in host:
        raise ValueError(f"registry host must be a bare host[:port], got {host!r}")
    if not is_repository_path(repo):
        raise ValueError(f"not an OCI repository path: {repo!r}")
    if not is_digest(digest):
        raise ValueError(f"not a content digest: {digest!r}")
    return f"{host}/{repo}@{digest}"


def is_repository_path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > REPOSITORY_MAX_LENGTH:
        return False
    return REPOSITORY_PATTERN.match(value) is not None


def is_digest(value: object) -> bool:
    return isinstance(value, str) and DIGEST_PATTERN.match(value) is not None


def registry_host(url: str) -> str:
    """``host[:port]`` of an external registry URL, without scheme, userinfo, or path."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"registry url {url!r} has no host")
    return f"{host}:{parts.port}" if parts.port is not None else host


def parse_timestamp(value: object) -> datetime | None:
    """RFC 3339 → aware UTC datetime, or ``None`` when absent or unparseable.

    Registries disagree on precision and on ``Z`` versus ``+00:00``; a missing
    or odd timestamp costs the catalog a sort key, not the artifact.
    """

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def http_verify(ca_bundle_path: str) -> ssl.SSLContext | bool:
    """``verify=`` for an httpx client: the deployment's CA bundle when configured, else system trust.

    Dev clusters front the registry with a gateway-issued certificate from a
    private CA, so the bundle is a file path from values. httpx 0.28 wants an
    ``SSLContext`` rather than a path.
    """

    if not ca_bundle_path:
        return True
    return ssl.create_default_context(cafile=ca_bundle_path)


def _http_url(value: str, *, field_name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{field_name} must be an http(s) URL with a host, got {value!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"{field_name} must not carry a query or fragment, got {value!r}")
    return value.rstrip("/")


class CogRegistryCredentials(BaseModel):
    """Robot/service credential presented to the registry.

    The password is populated from a mounted Secret, never from values, which
    is exactly why a username without a password is refused below: it is the
    shape a missing or misnamed Secret mount produces.
    """

    username: str = ""
    password: str = ""

    @field_validator("username", "password", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _both_or_neither(self) -> Self:
        if bool(self.username) != bool(self.password):
            raise ValueError(
                "credentials.username and credentials.password must be set together: one without the "
                "other is what a missing Secret mount looks like, and the registry would answer 401 on "
                "the first sweep instead of at startup"
            )
        return self

    @property
    def configured(self) -> bool:
        return bool(self.username)

    def __repr_args__(self):  # type: ignore[override]
        # Never let the password reach a log or a traceback through repr() or
        # str() of this model or of a config that nests it.
        yield "username", self.username
        yield "password", "***" if self.password else ""


class CogRegistrySourceConfig(BaseModel):
    """One registry the hub indexes. Shape shared with the ``cogs:`` config block (#87)."""

    id: str
    kind: RegistryKind
    url: str
    """External registry URL; its host is the identity in ``<host>/<repo>@<digest>``."""
    api_url: str = ""
    """Optional in-cluster override for the vendor listing API (and the OCI endpoints) — never an identity."""
    token_url: str = ""
    """Optional in-cluster bearer-token endpoint handed to the generic OCI client."""
    projects: list[str] = Field(default_factory=list)
    repositories: list[str] = Field(default_factory=list)
    index_url: str = ""
    ca_bundle_path: str = ""
    credentials: CogRegistryCredentials = Field(default_factory=CogRegistryCredentials)
    webhook_secret: str = ""
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator(
        "id", "url", "api_url", "token_url", "index_url", "ca_bundle_path", "webhook_secret", mode="before"
    )
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not value:
            raise ValueError("registry source id must not be empty")
        if len(value) > SOURCE_ID_MAX_LENGTH or not SOURCE_ID_PATTERN.match(value):
            raise ValueError(
                f"registry source id {value!r} must match {SOURCE_ID_PATTERN.pattern} "
                f"(at most {SOURCE_ID_MAX_LENGTH} characters); it is stored with every indexed row"
            )
        return value

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _http_url(value, field_name="url")

    @field_validator("api_url", "token_url", "index_url")
    @classmethod
    def _check_optional_urls(cls, value: str, info: Any) -> str:
        return _http_url(value, field_name=info.field_name) if value else value

    @field_validator("projects")
    @classmethod
    def _check_projects(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for project in value:
            # pydantic has already enforced str; only the grammar is ours.
            project = project.strip()
            if not PROJECT_PATTERN.match(project):
                raise ValueError(
                    f"projects entry {project!r} is not a registry project name "
                    "(one lowercase OCI repository component, no '/')"
                )
            if project in cleaned:
                raise ValueError(f"projects lists {project!r} twice")
            cleaned.append(project)
        return cleaned

    @field_validator("repositories")
    @classmethod
    def _check_repositories(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for repo in value:
            repo = repo.strip()
            if not is_repository_path(repo):
                raise ValueError(
                    f"repositories entry {repo!r} is not an OCI repository path "
                    "(lowercase 'project/name' components, no leading '/', no '..')"
                )
            if repo in cleaned:
                raise ValueError(f"repositories lists {repo!r} twice")
            cleaned.append(repo)
        return cleaned

    @model_validator(mode="after")
    def _check_kind_shape(self) -> Self:
        # Each kind has fields that only it reads. A field from the other kind
        # is not ignored: it almost always means the kind is mistyped, and the
        # source would then start and index nothing (or the wrong thing).
        label = f"registry source {self.id!r} (kind {self.kind!r})"
        if self.kind == "harbor":
            if not self.projects:
                raise ValueError(f"{label} requires at least one entry in projects: enumeration is per project")
            for name in ("repositories", "index_url"):
                if getattr(self, name):
                    raise ValueError(f"{label} does not read {name}; that field belongs to kind 'static'")
        elif self.kind == "static":
            if not self.repositories and not self.index_url:
                raise ValueError(f"{label} requires repositories and/or index_url: it has no listing API to ask")
            for name in ("projects", "api_url"):
                if getattr(self, name):
                    raise ValueError(f"{label} does not read {name}; that field belongs to kind 'harbor'")
            if self.webhook_secret:
                raise ValueError(f"{label} has no webhook; webhook_secret is not read")
        return self


OCIClientFactory = Callable[..., OCIClient]
"""``OCIClient``'s constructor signature; tests inject a fake."""


def build_registry_sources(
    configs: Sequence[CogRegistrySourceConfig],
    *,
    oci_client_factory: OCIClientFactory = OCIClient,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> list[RegistrySource]:
    """Instantiate one adapter per configured source, or refuse to start.

    ``make_app`` calls this once so that a duplicate id or an unsupported kind
    fails the rollout rather than the first sweep. ``http_transport`` exists
    for tests (``httpx.MockTransport``) and is handed to every adapter's own
    HTTP client and to the OCI client factory.
    """

    # Adapters import this module for the protocol and config types, so the
    # dispatch imports them lazily. This function is the only place outside
    # ``cogs/adapters/`` that names an adapter.
    from .adapters.harbor import HarborRegistrySource
    from .adapters.static import StaticRegistrySource

    seen: dict[str, int] = {}
    sources: list[RegistrySource] = []
    for index, config in enumerate(configs):
        if config.id in seen:
            raise RuntimeError(
                f"cogs.registry.sources[{index}] reuses id {config.id!r}, already taken by sources[{seen[config.id]}]: "
                "the id is stored with every indexed row, so two sources sharing one would make their "
                "artifacts indistinguishable. Give each source a distinct id."
            )
        seen[config.id] = index
        if config.kind == "harbor":
            source: RegistrySource = HarborRegistrySource(
                config, oci_client_factory=oci_client_factory, http_transport=http_transport
            )
        elif config.kind == "static":
            source = StaticRegistrySource(config, oci_client_factory=oci_client_factory, http_transport=http_transport)
        else:
            raise RuntimeError(
                f"cogs.registry.sources[{index}] ({config.id!r}) has unsupported kind {config.kind!r}: expected "
                "exactly 'harbor' (enumerate projects through the registry's REST API and translate its webhooks) or "
                "'static' (a configured repository list and/or a catalog.v1.json index; generic OCI only)."
            )
        sources.append(source)
    return sources


def parse_registry_event(
    sources: Sequence[RegistrySource], request: WebhookRequest
) -> tuple[RegistrySource, list[RegistryEvent]] | None:
    """Hand a delivery to each source in order; the first that claims it owns it.

    ``None`` when no source recognizes the payload, so the receiver can answer
    "ignored" without knowing any vendor format.
    """

    for source in sources:
        events = source.parse_event(request)
        if events is not None:
            return source, events
    return None
