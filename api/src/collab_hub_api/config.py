import sys
from typing import Any, Literal, Self

import l2sl
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .frames.account_provisioning import DisabledServiceAccessGranter, ServiceAccessGranter
from .frames.active_state import (
    ActiveFrameStore,
    DisabledActiveFrameStore,
    InMemoryActiveFrameStore,
    PostgresActiveFrameStore,
)
from .frames.collab_schema import check_collab_schema_version, run_collab_schema_migrations
from .frames.db import (
    DEFAULT_MAX_SIZE,
    DEFAULT_MAX_WAITING,
    DEFAULT_MIN_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_WAITING_LIMIT,
    POOL_SIZE_LIMIT,
    TIMEOUT_SECONDS_LIMIT,
    PostgresPools,
)
from .frames.groups import (
    FrameGroupStore,
    InMemoryFrameGroupStore,
    PostgresFrameGroupStore,
    UnavailableFrameGroupStore,
)
from .frames.history import (
    FrameHistoryStore,
    InMemoryFrameHistoryStore,
    PostgresFrameHistoryStore,
    UnavailableFrameHistoryStore,
)
from .frames.invitation_email import (
    ConfiguredInvitationEmailDelivery,
    DisabledInvitationEmailDelivery,
    SesInvitationEmailProvider,
    validate_accept_url,
    validate_mailbox,
)
from .frames.invitations import (
    InvitationService,
    PostgresInvitationService,
    UnavailableInvitationService,
)
from .frames.keycloak_service_access import KeycloakServiceAccessGranter
from .frames.orgs import InMemoryOrgStore, OrgStore, PostgresOrgStore, UnavailableOrgStore
from .frames.store import FrameStore, LocalFsFrameStore, S3FrameStore
from .frames.usage import (
    InMemoryUsageStore,
    PostgresUsageStore,
    UnavailableUsageStore,
    UsageStore,
)
from .tasks.store import InMemoryTaskStore, PostgresTaskStore
from .user_directory import DisabledUserDirectoryClient, KeycloakUserDirectoryClient, UserDirectoryClient


def interactive_session() -> bool:
    return sys.stdout.isatty()


class ServerConfig(BaseModel):
    """How uvicorn is bound and what it believes about the hop in front of it.

    ``proxy_headers`` stays False here because the process default has to be
    the safe one for a server reached directly (local runs, port-forwards):
    trusting ``X-Forwarded-*`` from an arbitrary client lets it dictate the
    scheme and the client address that end up in logs. Behind a proxy that
    always sets those headers — the Nebari gateway or the chart's Ingress —
    the correct value is True, and the Helm chart turns it on by default for
    exactly that reason (``server.proxyHeaders``). ``forwarded_allow_ips``
    bounds which peers may set them; ``["*"]`` is only sound when the pod is
    unreachable except through the proxy.
    """

    hostname: str = "127.0.0.1"
    port: int = 8000
    proxy_headers: bool = False
    forwarded_allow_ips: list[str] = Field(default_factory=lambda: ["*"])
    root_path: str = ""


class LoggingConfig(BaseModel):
    level: l2sl.LogLevel = l2sl.LogLevel("info")
    as_json: bool = Field(default_factory=lambda: not interactive_session())


class ObservabilityConfig(BaseModel):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class CORSConfig(BaseModel):
    """Cross-origin rules for browser callers.

    ``allowed_origins`` keeps its historical ``["*"]`` default so that
    upgrading an existing deployment — or anyone running this image without the
    Helm chart — does not silently lose browser callers. A wildcard is still
    the wrong answer for a multi-tenant hub: native clients send no ``Origin``
    and the server's own pages are same-origin, so the grant buys nothing and
    hands every site on the internet a cross-origin call path. The chart
    therefore narrows it to ``[]`` for standalone/ingress exposure, which is
    new and has no installed base; see ``security.cors.allowedOrigins``.
    """

    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    allowed_headers: list[str] = Field(default_factory=lambda: ["Authorization", "Content-Type"])
    allow_credentials: bool = False

    @model_validator(mode="after")
    def _reject_wildcard_with_credentials(self) -> Self:
        # Starlette echoes the request Origin when allow_origins is ["*"] and
        # credentials are allowed, so the combination is not "wildcard without
        # cookies" — it is "every site may make credentialed calls".
        if "*" in self.allowed_origins and self.allow_credentials:
            raise ValueError(
                "security.cors.allowed_origins may not contain '*' when "
                "allow_credentials is true; name the origins explicitly"
            )
        return self


PathAccess = Literal["public", "authenticated"]
"""How a request path is protected.

``public`` reaches the route with no credentials. ``authenticated`` requires a
verified IdToken cookie or bearer token (the same check the API routes use)
before the request is routed at all.

Role-scoped levels (operator-only ``/admin``, owner-only ``/org``) belong with
the browser surface in issue #88; they are deliberately absent here rather than
accepted-and-ignored, so a map can never claim a protection the server does not
enforce.
"""


class PathRule(BaseModel):
    """One entry of the per-path protection map."""

    path: str
    match: Literal["prefix", "exact"] = "prefix"
    access: PathAccess = "authenticated"

    @model_validator(mode="after")
    def _check_path(self) -> Self:
        if not self.path.startswith("/"):
            raise ValueError(f"security.paths entry {self.path!r} must start with '/'")
        return self


def recommended_path_rules() -> list[PathRule]:
    """The map a hardened deployment runs, paired with ``default_access="authenticated"``.

    Deliberately *not* the process default. Enforcing protection by default
    would change how every already-running deployment behaves the moment it is
    upgraded: an in-cluster Prometheus scrape of ``/metrics`` would start
    answering 401, and so would the hub landing-page link to ``/`` unless that
    install happens to have usable IdToken JWKS settings. Protection is opted
    into instead, and the Helm chart opts in automatically for
    standalone/ingress exposure, which is new and has no installed base
    (``security.enforce``).

    ``/health`` and ``/health/db`` stay public because kubelet probes and
    uptime checks carry no credentials: a hardened map that drops them stops
    the pod passing its own probes.
    """

    return [
        PathRule(path="/health", match="exact", access="public"),
        PathRule(path="/health/db", match="exact", access="public"),
        PathRule(path="/", match="exact", access="authenticated"),
        PathRule(path="/metrics", match="exact", access="authenticated"),
    ]


class SecurityConfig(BaseModel):
    cors: CORSConfig = Field(default_factory=CORSConfig)
    # The protection map is data, not code: the server enforces whatever the
    # map says, so a new public page (issue #88's /invite/accept) or an
    # in-cluster metrics scrape is a values change, not a code change.
    #
    # Empty, with default_access "public", so an unconfigured server behaves
    # exactly as it did before the map existed — route dependencies protect the
    # API and nothing else is enforced here. recommended_path_rules() is what a
    # hardened deployment sets; the chart renders it for ingress exposure.
    paths: list[PathRule] = Field(default_factory=list)
    # Applied to any path no rule matches.
    default_access: PathAccess = "public"


WEB_SESSION_LIFETIME_CEILING_SECONDS = 8 * 3600
"""Hard ceiling on a web session's absolute lifetime, and its default.

Not a suggestion: the session cookie is stateless and cannot be revoked
server-side before it expires, so this number *is* the window in which a
cookie captured before sign-out keeps asserting its holder's identity. The
documented risk argument rests on it, so the configuration may lower it and
may not raise it.
"""

WEB_SESSION_SECRET_MIN_LENGTH = 32
WEB_SESSION_SECRET_MIN_DISTINCT = 16
"""Floors for the signing secret.

Length alone admits ``"a" * 32`` and 32 spaces, which carry no entropy at all
while satisfying a "32 characters" rule. Counting distinct characters is a
crude proxy for entropy and is not a substitute for generating the value with
``secrets.token_urlsafe(32)``, but it does refuse the degenerate values a
human types when a config demands a long string. Whitespace is stripped first,
so padding cannot buy either floor.
"""


def _check_session_secret(secret: str) -> None:
    """Refuse a signing secret that is short or visibly patterned."""

    stripped = secret.strip()
    if len(stripped) < WEB_SESSION_SECRET_MIN_LENGTH:
        raise ValueError(
            f"web.session_secret must be at least {WEB_SESSION_SECRET_MIN_LENGTH} characters"
            " (whitespace stripped); it signs every browser session cookie. Generate it with"
            " `python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
        )
    if len(set(stripped)) < WEB_SESSION_SECRET_MIN_DISTINCT:
        raise ValueError(
            "web.session_secret has too few distinct characters to be a random secret; it"
            " signs every browser session cookie. Generate it with"
            " `python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
        )


class WebConfig(BaseModel):
    """The server-side browser surface (issue #88).

    Setting ``client_id`` enables the surface: it names the **confidential**
    OIDC client (``collab-web`` or similar) registered in the same Keycloak
    realm the bearer verifier trusts, distinct from the desktop's public
    ``apollo-desktop`` client. ``issuer_url`` may be left empty to inherit the
    bearer issuer (``FRAMES_BEARER_ISSUER``) — inheriting the *realm* is safe
    and deliberate; the token **audience** is never inherited and is always
    this client's own id (the issue #83 lesson).

    ``session_secret`` signs the session and transient cookies; it must be
    high-entropy and identical on every replica. ``public_base_url`` overrides
    request-derived redirect-URI construction for deployments whose forwarded
    headers are not trusted.
    """

    client_id: str = ""
    client_secret: str = ""
    issuer_url: str = ""
    scope: str = "openid email profile"
    session_secret: str = ""
    session_lifetime_seconds: int = Field(
        default=WEB_SESSION_LIFETIME_CEILING_SECONDS,
        gt=0,
        le=WEB_SESSION_LIFETIME_CEILING_SECONDS,
    )
    public_base_url: str = ""

    @field_validator(
        "client_id",
        "client_secret",
        "issuer_url",
        "session_secret",
        "public_base_url",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: Any) -> Any:
        """Normalize at the boundary so validation and use see the same value.

        These arrive from environment variables and YAML, both of which
        acquire stray whitespace easily. Validating a stripped value while
        *forwarding* the raw one is the worst of both: ``" secret "`` passed
        the "is it non-empty" check and was then POSTed to Keycloak with its
        spaces intact, which fails authentication with an error that names
        nothing useful. Stripping here means there is only one value.
        """

        return value.strip() if isinstance(value, str) else value

    @property
    def enabled(self) -> bool:
        return bool(self.client_id)

    @model_validator(mode="after")
    def _check_enabled_shape(self) -> Self:
        # Only the checks that need no environment: env fallbacks, transport
        # security, and the protection-map coherence check live in
        # web.surface.enforce_web_surface_preconditions, called by make_app.
        if not self.enabled:
            return self
        if not self.client_secret:
            raise ValueError(
                "web.client_id names a confidential client, which requires web.client_secret"
                " (a whitespace-only value is not a secret)"
            )
        _check_session_secret(self.session_secret)
        if "openid" not in self.scope.split():
            raise ValueError(
                f"web.scope must request the 'openid' scope, got {self.scope!r}: without it"
                " the token response carries no id_token and no sign-in can complete"
            )
        return self


class StorageConfig(BaseModel):
    frames_path: str = "/var/frames"


class FramesS3Config(BaseModel):
    bucket: str = ""
    prefix: str = "frames"
    endpoint_url: str = ""
    region: str = ""


class FramesPostgresPoolConfig(BaseModel):
    """Sizing, wait timeout, and waiter bound for the shared psycopg pools.

    One setting covers every pool the app creates (normally just the shared
    ``frames.postgres`` one). ``timeout_seconds`` bounds how long a request
    waits for a connection when the pool is exhausted or Postgres is down
    before failing with a 503; ``max_waiting`` bounds how many callers may be
    queued for a connection at once (0 disables the bound).

    The bounds are enforced here rather than left to psycopg: a pod that comes
    up with an unusable pool (``max_size`` below ``min_size``, a zero or
    negative timeout, an accidental ``max_size: 0``) is worse than one that
    refuses to start with a named configuration error.
    """

    min_size: int = Field(default=DEFAULT_MIN_SIZE, ge=0, le=POOL_SIZE_LIMIT)
    max_size: int = Field(default=DEFAULT_MAX_SIZE, ge=1, le=POOL_SIZE_LIMIT)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=TIMEOUT_SECONDS_LIMIT)
    max_waiting: int = Field(default=DEFAULT_MAX_WAITING, ge=0, le=MAX_WAITING_LIMIT)

    @model_validator(mode="after")
    def _check_pool_sizes(self) -> Self:
        if self.max_size < self.min_size:
            raise ValueError(
                f"frames.postgres.pool.max_size ({self.max_size}) must be greater than "
                f"or equal to min_size ({self.min_size})"
            )
        return self


class FramesPostgresConfig(BaseModel):
    """The single shared Postgres URL backing the relational frames features.

    Setting ``url`` lights up history and groups (required features) and is the
    fallback for active-state's Postgres backend. There is no per-feature
    ``disabled`` toggle — the only off state for history/groups is "no DB".
    """

    url: str = ""
    auto_migrate: bool = False
    pool: FramesPostgresPoolConfig = Field(default_factory=FramesPostgresPoolConfig)


class FramesActiveStatePostgresConfig(BaseModel):
    url: str = ""
    auto_migrate: bool = False


class FramesActiveStateConfig(BaseModel):
    backend: str = "disabled"
    postgres: FramesActiveStatePostgresConfig = Field(default_factory=FramesActiveStatePostgresConfig)


class FramesHistoryConfig(BaseModel):
    # Optional test/dev override only: "memory" forces the in-memory store.
    # Otherwise history rides the shared frames.postgres (503 when no URL is set).
    backend: str = ""


class FramesGroupsConfig(BaseModel):
    # Same override semantics as history: "memory" for tests/dev, else shared
    # frames.postgres (503 when no URL is set). No per-feature "disabled".
    backend: str = ""


class FramesUsageConfig(BaseModel):
    # Same override semantics as history: "memory" for tests/dev, else shared
    # frames.postgres (503 when no URL is set). No per-feature "disabled".
    backend: str = ""


class FramesOrgsConfig(BaseModel):
    # Same override semantics as history: "memory" is a test/dev override,
    # otherwise organization membership rides the shared frames.postgres. There
    # is deliberately no chart value for this: an in-memory membership table is
    # process-local, so on a deployment it would authorize differently on every
    # replica and forget every removal on restart.
    backend: str = ""


class FramesEmailSesConfig(BaseModel):
    sender_address: str = ""
    region: str = ""
    configuration_set: str = ""
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class FramesEmailConfig(BaseModel):
    provider: Literal["disabled", "ses"] = "disabled"
    accept_url: str = ""
    app_instructions: str = ""
    """How an invitee gets the desktop app, in their own words in the email.

    Required once a provider is set, and validated below with the rest. The
    approved copy (#153) has a step telling the reader to install the app, and
    nothing in the service can know how a given deployment distributes its
    build — so there is no truthful default, and a deployment that enables mail
    without setting this would send a placeholder to a real person. Failing at
    startup is the alternative, which is why it is validated rather than
    defaulted.
    """

    ses: FramesEmailSesConfig = Field(default_factory=FramesEmailSesConfig)

    @model_validator(mode="after")
    def _validate_enabled_provider(self) -> Self:
        if self.provider == "disabled":
            return self

        missing = [
            name
            for name, value in {
                "accept_url": self.accept_url,
                "app_instructions": self.app_instructions,
                "ses.sender_address": self.ses.sender_address,
                "ses.region": self.ses.region,
                "ses.configuration_set": self.ses.configuration_set,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing required invitation email config: {', '.join(missing)}")
        validate_accept_url(self.accept_url)
        validate_mailbox(self.ses.sender_address)
        return self


class ServiceAccessKeycloakConfig(BaseModel):
    """Where the membership-writing credential lives (#180).

    Its own block rather than reuse of ``user_directory.keycloak``, because
    they are different credentials on purpose: that one is read-only and this
    one writes. Sharing the configuration would invite sharing the secret, and
    the read credential's whole value is that it cannot write.
    """

    issuer_url: str = ""
    token_url: str = ""
    admin_api_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    group_ids: dict[str, str] = {}
    """Group path to Keycloak group id, for paths this credential cannot look up.

    Keycloak's membership endpoint needs an id. Resolving a path to one requires
    *reading* groups, and the credential this block configures is deliberately
    write-only: on collab-hub its measured boundary is 403 on every read,
    ``GET /groups`` included. Supplying the id keeps it that narrow, and means
    startup makes no call to the identity provider at all.

    A path absent from this mapping is looked up instead, which works only where
    the credential holds group-read authority. Both are supported because both
    are legitimate; only one of them needs a wider credential.
    """


class FramesServiceAccessConfig(BaseModel):
    """Which identity-provider groups an accepted invitation grants (#180).

    A list rather than a boolean, and **empty by default**, because the two
    questions this answers change independently: whether a deployment grants
    anything automatically, and what it grants. A deployment that has not
    decided grants nothing, which is also the only safe default -- the
    behaviour this replaced granted membership at account creation and
    therefore reached anyone who self-registered
    (an internal issue).

    Configuration rather than code because the current answer is temporary.
    The initial invites grant everyone who accepts; narrowing that later must
    be a values change, not a release.

    Names are group paths as the identity provider spells them (``/llm``, or
    ``/services/llm`` if service groups are nested). Nesting does not change
    what appears in a token on this deployment -- the group-membership mapper
    is configured ``full.path: false``, so the claim carries bare names either
    way -- but the *grant* addresses the group by path.
    """

    grant_on_acceptance: list[str] = Field(default_factory=list)
    keycloak: ServiceAccessKeycloakConfig = Field(default_factory=ServiceAccessKeycloakConfig)

    @model_validator(mode="after")
    def _validate(self) -> "FramesServiceAccessConfig":
        for name in self.grant_on_acceptance:
            if not name or not name.strip():
                raise ValueError("frames.service_access.grant_on_acceptance entries must not be blank")
            if name != name.strip():
                raise ValueError(
                    f"frames.service_access.grant_on_acceptance entry {name!r} has surrounding whitespace: "
                    "a group path is matched exactly, and a stray space fails at grant time rather than here"
                )
        duplicates = {name for name in self.grant_on_acceptance if self.grant_on_acceptance.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"frames.service_access.grant_on_acceptance lists {sorted(duplicates)} more than once"
            )
        return self


class FramesInvitationsConfig(BaseModel):
    """What invitation acceptance requires of the accepter's identity."""

    require_verified_email: bool = True
    """Whether Gate B additionally requires the identity provider to have
    verified the address (#67).

    **True is the default and must stay the default.** Turning it off is a
    deliberate deployment trade, and a default that silently weakened an
    existing deployment on upgrade would be the wrong kind of convenient.

    Gate B is two checks: the accepter's address is verified, *and* it equals
    the invited address. Setting this false drops the first and keeps the
    second, on the argument that the invitation token is itself proof of
    mailbox control — it is a 256-bit secret delivered only to the invited
    address, which is exactly what a verification link proves. Requiring both
    is defence in depth rather than one necessary check.

    **What is given up, stated so a deployment chooses it knowingly.** A
    forwarded or shared-mailbox invitation becomes usable by whoever received
    it: today they cannot accept, because they cannot verify an address they
    do not control, and the invitation simply goes unused. With this false they
    can, and the account they end up with carries the invited person's address
    permanently -- an identity-confusion problem in the member list, not only
    an access one.

    **When to leave it true.** Any deployment whose invitees arrive through an
    identity provider with ``trustEmail``: those accounts are already verified
    on creation, so the check costs nothing and the invitee never sees a
    verification step. Turning it off buys nothing there.

    **When false is reasonable.** A password-based deployment with no identity
    provider, a known invitee list, and a verification round trip that is
    friction rather than assurance.
    """


class FramesConfig(BaseModel):
    storage_backend: str = "local"
    s3: FramesS3Config = Field(default_factory=FramesS3Config)
    postgres: FramesPostgresConfig = Field(default_factory=FramesPostgresConfig)
    active_state: FramesActiveStateConfig = Field(default_factory=FramesActiveStateConfig)
    history: FramesHistoryConfig = Field(default_factory=FramesHistoryConfig)
    groups: FramesGroupsConfig = Field(default_factory=FramesGroupsConfig)
    usage: FramesUsageConfig = Field(default_factory=FramesUsageConfig)
    orgs: FramesOrgsConfig = Field(default_factory=FramesOrgsConfig)
    email: FramesEmailConfig = Field(default_factory=FramesEmailConfig)
    service_access: FramesServiceAccessConfig = Field(default_factory=FramesServiceAccessConfig)
    invitations: FramesInvitationsConfig = Field(default_factory=FramesInvitationsConfig)
    mcp_session_manager_enabled: bool = True


class GoogleConnectorConfig(BaseModel):
    broker_token_url: str = ""
    drive_api_base_url: str = "https://www.googleapis.com/drive/v3"
    gmail_api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    calendar_api_base_url: str = "https://www.googleapis.com/calendar/v3"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0


class SlackConnectorConfig(BaseModel):
    broker_token_url: str = ""
    api_base_url: str = "https://slack.com/api"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0


class GitHubConnectorConfig(BaseModel):
    broker_token_url: str = ""
    api_base_url: str = "https://api.github.com"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0


class ConnectorsConfig(BaseModel):
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)
    slack: SlackConnectorConfig = Field(default_factory=SlackConnectorConfig)
    github: GitHubConnectorConfig = Field(default_factory=GitHubConnectorConfig)


class KeycloakUserDirectoryConfig(BaseModel):
    issuer_url: str = ""
    token_url: str = ""
    admin_api_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""


class UserDirectoryConfig(BaseModel):
    enabled: bool = False
    provider: str = "keycloak"
    keycloak: KeycloakUserDirectoryConfig = Field(default_factory=KeycloakUserDirectoryConfig)


class TasksConfig(BaseModel):
    backend: str = "memory"
    postgres_url: str = ""
    auto_migrate: bool = False


class BaseConfig(BaseSettings):
    server: ServerConfig = Field(default_factory=ServerConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    frames: FramesConfig = Field(default_factory=FramesConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    user_directory: UserDirectoryConfig = Field(default_factory=UserDirectoryConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)


class Config(BaseConfig):
    model_config = SettingsConfigDict(env_prefix="COLLAB_HUB_API__", env_nested_delimiter="__")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings

    @classmethod
    def parse(cls, obj: dict[str, Any] | None = None) -> Self:
        if obj is None:
            obj = {}
        return cls.model_validate(obj)


def build_frames_store(config: BaseConfig) -> FrameStore:
    if config.frames.storage_backend == "local":
        return LocalFsFrameStore(config.storage.frames_path)
    if config.frames.storage_backend == "s3":
        if not config.frames.s3.bucket:
            raise RuntimeError("COLLAB_HUB_API__FRAMES__S3__BUCKET is required for S3 frame storage")
        return S3FrameStore(
            bucket=config.frames.s3.bucket,
            prefix=config.frames.s3.prefix,
            endpoint_url=config.frames.s3.endpoint_url or None,
            region_name=config.frames.s3.region or None,
        )
    raise RuntimeError(f"Unsupported frame storage backend: {config.frames.storage_backend}")


def build_postgres_pools(config: BaseConfig) -> PostgresPools:
    """Create the app-wide pool registry all Postgres stores draw from.

    Stores sharing a database URL share one pool; the registry is opened at
    app startup and closed at shutdown by the app lifespan.
    """

    pool = config.frames.postgres.pool
    return PostgresPools(
        min_size=pool.min_size,
        max_size=pool.max_size,
        timeout_seconds=pool.timeout_seconds,
        max_waiting=pool.max_waiting,
    )


def build_active_frame_store(config: BaseConfig, pools: PostgresPools) -> ActiveFrameStore:
    backend = config.frames.active_state.backend
    if backend == "disabled":
        return DisabledActiveFrameStore()
    if backend == "memory":
        return InMemoryActiveFrameStore()
    if backend == "postgres":
        # Use active-state's own URL if given, else fall back to the shared
        # frames.postgres URL — configuring one shared URL lights up
        # active-state, history, and groups together.
        own = config.frames.active_state.postgres
        url = own.url or config.frames.postgres.url
        if not url:
            raise RuntimeError(
                "active_state.backend=postgres requires COLLAB_HUB_API__FRAMES__ACTIVE_STATE__POSTGRES__URL "
                "or the shared COLLAB_HUB_API__FRAMES__POSTGRES__URL"
            )
        auto_migrate = own.auto_migrate if own.url else config.frames.postgres.auto_migrate
        return PostgresActiveFrameStore(pools.database(url), auto_migrate=auto_migrate)
    raise RuntimeError(f"Unsupported active Frame state backend: {backend}")


def build_history_store(config: BaseConfig, pools: PostgresPools) -> FrameHistoryStore:
    # No per-feature "disabled": "memory" is a test/dev override; otherwise ride
    # the shared frames.postgres, or an unavailable store (→ 503) when no DB.
    if config.frames.history.backend == "memory":
        return InMemoryFrameHistoryStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameHistoryStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameHistoryStore()


def build_usage_store(config: BaseConfig, pools: PostgresPools) -> UsageStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503 on reads/events). No per-feature "disabled".
    if config.frames.usage.backend == "memory":
        return InMemoryUsageStore()
    url = config.frames.postgres.url
    if url:
        return PostgresUsageStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableUsageStore()


def build_group_store(config: BaseConfig, pools: PostgresPools) -> FrameGroupStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503). No per-feature "disabled".
    if config.frames.groups.backend == "memory":
        return InMemoryFrameGroupStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameGroupStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameGroupStore()


def build_org_store(config: BaseConfig, pools: PostgresPools) -> OrgStore:
    # Mirrors build_history_store: "memory" test/dev override, else the shared
    # frames.postgres, else a store that fails closed. Unlike the other
    # features there is no 503-and-carry-on story here — membership is an
    # authorization input, so make_app refuses to *start* a membership-
    # resolving deployment whose store is the unavailable one.
    if config.frames.orgs.backend == "memory":
        return InMemoryOrgStore()
    url = config.frames.postgres.url
    if url:
        return PostgresOrgStore(pools.database(url))
    return UnavailableOrgStore()


def build_invitation_service(config: BaseConfig, pools: PostgresPools) -> InvitationService:
    """Build the invitation lifecycle service, or the one that refuses (503).

    Deliberately **not** mirroring the other builders' ``"memory"`` override:
    there is no in-memory invitation backend and there will not be one. What
    this service promises — a token that cannot be redeemed twice, exactly one
    organization under concurrent acceptance, a mutation that commits with its
    audit row or not at all — is the ``FOR UPDATE`` lock, the membership
    primary key, and the transaction. An in-memory stand-in could reproduce
    the method signatures and none of the guarantees, and a dev deployment
    that appeared to work would be the worst possible place to discover that.
    """

    url = config.frames.postgres.url
    if url:
        return PostgresInvitationService(
            pools.database(url),
            require_verified_email=config.frames.invitations.require_verified_email,
        )
    return UnavailableInvitationService()


def build_invitation_email_delivery(
    config: BaseConfig,
) -> ConfiguredInvitationEmailDelivery | DisabledInvitationEmailDelivery:
    """Build the provider seam without making a network or credential call."""

    email = config.frames.email
    if email.provider == "disabled":
        return DisabledInvitationEmailDelivery()
    if email.provider == "ses":
        provider = SesInvitationEmailProvider(
            sender_address=email.ses.sender_address,
            region=email.ses.region,
            configuration_set=email.ses.configuration_set,
            request_timeout_seconds=email.ses.request_timeout_seconds,
        )
        return ConfiguredInvitationEmailDelivery(
            provider,
            accept_url=email.accept_url,
            app_instructions=email.app_instructions,
            # The same value the acceptance check reads, so the copy and the
            # rule it describes cannot disagree.
            require_verified_email=config.frames.invitations.require_verified_email,
        )
    raise RuntimeError(f"Unsupported invitation email provider: {email.provider}")


def migrate_collab_schema(config: BaseConfig, pools: PostgresPools) -> bool:
    """Bring the ``collab_`` tenancy tables up to date, if configured to.

    Unlike the frames features there is no per-store ``_ensure_schema`` to hang
    this on: the ``collab_`` tables are migrated by one versioned, advisory-lock
    guarded runner (see :mod:`.frames.collab_schema`). The trigger is the same
    pair of settings the stores use — the shared ``frames.postgres.url`` plus
    ``frames.postgres.auto_migrate`` — so a deployment that already opts into
    auto-migration gets these tables with no new switch, and one that migrates
    out of band still does.

    Returns whether the migration ran, which is what the caller can meaningfully
    assert; failures propagate exactly as the stores' ``auto_migrate`` does.
    """

    url = config.frames.postgres.url
    if not url or not config.frames.postgres.auto_migrate:
        return False
    run_collab_schema_migrations(pools.database(url))
    return True


def preflight_collab_schema(config: BaseConfig, pools: PostgresPools) -> int | None:
    """Refuse to start against a ``collab_`` schema older than this build needs.

    Issue #96, landed with its first consumer: until something reads these
    tables a version check has nothing to protect, and once something does, the
    alternative to checking is a ``relation does not exist`` traceback from
    whichever authenticated request touched them first. Called by ``make_app``
    only when membership resolution is on, and only when a shared Postgres URL
    is configured — with neither, no ``collab_`` table is ever read.

    Returns the applied version, ``None`` when the database was unreachable
    (not fatal, see :func:`~.frames.collab_schema.check_collab_schema_version`),
    and raises when the schema is behind.
    """

    url = config.frames.postgres.url
    if not url:
        return None
    return check_collab_schema_version(
        pools.database(url),
        auto_migrate=config.frames.postgres.auto_migrate,
    )


def build_service_access_granter(config: BaseConfig) -> ServiceAccessGranter:
    """The membership-writing seam, or the one that refuses (#180).

    Three states, and only the third does anything:

    * **nothing configured to grant** -- the default. Returns the disabled
      granter. Nothing should hand out service access because a deployment
      forgot to say otherwise; that is the shape of the behaviour this
      replaced (an internal issue);
    * **groups configured, credential incomplete** -- refuses to start. A
      deployment that says "grant `/llm` on acceptance" and cannot is
      misconfigured, and the invitees it would accept meanwhile are exactly
      who pays for discovering that later;
    * **both** -- builds the granter and **resolves every configured group path
      to an id immediately**, so a typo fails in front of whoever deployed it
      rather than once per invitee, silently, at acceptance time.
    """

    groups = tuple(config.frames.service_access.grant_on_acceptance)
    if not groups:
        return DisabledServiceAccessGranter()

    keycloak = config.frames.service_access.keycloak
    token_url = keycloak.token_url or _keycloak_token_url(keycloak.issuer_url)
    missing = [
        name
        for name, value in {
            "issuer_url": keycloak.issuer_url,
            "token_url": token_url,
            "admin_api_base_url": keycloak.admin_api_base_url,
            "client_id": keycloak.client_id,
            "client_secret": keycloak.client_secret,
        }.items()
        if not value
    ]
    if missing:
        fields = ", ".join(f"frames.service_access.keycloak.{name}" for name in missing)
        raise RuntimeError(
            f"frames.service_access.grant_on_acceptance names {list(groups)} but the credential "
            f"to grant them is incomplete: {fields}. Either configure the credential or clear the "
            f"group list -- a deployment that promises service access and cannot grant it strands "
            f"every invitee it accepts."
        )

    unknown = sorted(set(keycloak.group_ids) - set(groups))
    if unknown:
        raise RuntimeError(
            f"frames.service_access.keycloak.group_ids names {unknown}, which "
            f"frames.service_access.grant_on_acceptance does not grant. A mapping for a path "
            f"nothing grants is a typo in one of the two lists, and the harmless-looking "
            f"reading -- an unused entry -- is the one that leaves the real path unmapped."
        )
    malformed = sorted(
        path for path, group_id in keycloak.group_ids.items() if not group_id.strip() or "/" in group_id
    )
    if malformed:
        raise RuntimeError(
            f"frames.service_access.keycloak.group_ids has no usable id for {malformed}. "
            f"The value must be Keycloak's group id, not the path again -- checked only for "
            f"emptiness and for looking like a path, because the id's format is Keycloak's to "
            f"choose and a stricter rule here would be this file inventing a contract."
        )

    granter = KeycloakServiceAccessGranter(
        token_url=token_url,
        admin_api_base_url=keycloak.admin_api_base_url,
        client_id=keycloak.client_id,
        client_secret=keycloak.client_secret,
        group_paths=groups,
        group_ids=keycloak.group_ids,
    )
    # Resolved now rather than on first use: this call is the startup gate. A
    # configured group that does not exist raises, naming the path.
    #
    # When every path has a configured id this makes **no request** -- which is
    # the point of allowing them. A write-only credential cannot perform the
    # lookup at all (403 on every read), and even where it could, making startup
    # depend on the identity provider means a Keycloak blip during a rollout
    # leaves no pods rather than degraded ones.
    #
    # Closed on that path, because the granter already owns an `httpx.Client` by
    # the time it is asked to resolve. Refusing to start is the intended
    # outcome; leaking a connection pool on the way out is not, and a
    # misconfigured deployment restarting in a loop would leak one per attempt
    # (raised in review of #183).
    try:
        granter.resolve_groups()
    except Exception:
        granter.close()
        raise
    return granter


def build_user_directory_client(config: BaseConfig) -> UserDirectoryClient:
    if not config.user_directory.enabled:
        return DisabledUserDirectoryClient()
    if config.user_directory.provider != "keycloak":
        raise RuntimeError(f"Unsupported user directory provider: {config.user_directory.provider}")

    keycloak = config.user_directory.keycloak
    token_url = keycloak.token_url or _keycloak_token_url(keycloak.issuer_url)
    missing = [
        name
        for name, value in {
            "issuer_url": keycloak.issuer_url,
            "token_url": token_url,
            "admin_api_base_url": keycloak.admin_api_base_url,
            "client_id": keycloak.client_id,
            "client_secret": keycloak.client_secret,
        }.items()
        if not value
    ]
    if missing:
        fields = ", ".join(f"user_directory.keycloak.{name}" for name in missing)
        raise RuntimeError(f"Missing required Keycloak user directory config: {fields}")

    return KeycloakUserDirectoryClient(
        token_url=token_url,
        admin_api_base_url=keycloak.admin_api_base_url,
        client_id=keycloak.client_id,
        client_secret=keycloak.client_secret,
    )


def build_task_store(config: BaseConfig, pools: PostgresPools) -> InMemoryTaskStore | PostgresTaskStore:
    if config.tasks.backend == "memory":
        return InMemoryTaskStore()
    if config.tasks.backend != "postgres":
        raise RuntimeError(f"Unsupported task storage backend: {config.tasks.backend}")
    url = config.tasks.postgres_url or config.frames.postgres.url
    if not url:
        raise RuntimeError(
            "tasks.backend=postgres requires COLLAB_HUB_API__TASKS__POSTGRES_URL "
            "or the shared COLLAB_HUB_API__FRAMES__POSTGRES__URL. "
            "Set tasks.backend=memory for explicit non-durable local development."
        )
    return PostgresTaskStore(
        pools.database(url), auto_migrate=config.tasks.auto_migrate or config.frames.postgres.auto_migrate
    )


def _keycloak_token_url(issuer_url: str) -> str:
    issuer_url = issuer_url.rstrip("/")
    if not issuer_url:
        return ""
    return f"{issuer_url}/protocol/openid-connect/token"
