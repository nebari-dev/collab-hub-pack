import sys
from typing import Any, Self

import l2sl
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .frames.active_state import (
    ActiveFrameStore,
    DisabledActiveFrameStore,
    InMemoryActiveFrameStore,
    PostgresActiveFrameStore,
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
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    allowed_headers: list[str] = Field(default_factory=lambda: ["Authorization", "Content-Type"])
    allow_credentials: bool = False


class SecurityConfig(BaseModel):
    cors: CORSConfig = Field(default_factory=CORSConfig)


class StorageConfig(BaseModel):
    frames_path: str = "/var/frames"


class FramesS3Config(BaseModel):
    bucket: str = ""
    prefix: str = "frames"
    endpoint_url: str = ""
    region: str = ""


class FramesPostgresConfig(BaseModel):
    """The single shared Postgres URL backing the relational frames features.

    Setting ``url`` lights up history and groups (required features) and is the
    fallback for active-state's Postgres backend. There is no per-feature
    ``disabled`` toggle — the only off state for history/groups is "no DB".
    """

    url: str = ""
    auto_migrate: bool = False


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


class FramesConfig(BaseModel):
    storage_backend: str = "local"
    s3: FramesS3Config = Field(default_factory=FramesS3Config)
    postgres: FramesPostgresConfig = Field(default_factory=FramesPostgresConfig)
    active_state: FramesActiveStateConfig = Field(default_factory=FramesActiveStateConfig)
    history: FramesHistoryConfig = Field(default_factory=FramesHistoryConfig)
    groups: FramesGroupsConfig = Field(default_factory=FramesGroupsConfig)
    usage: FramesUsageConfig = Field(default_factory=FramesUsageConfig)
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


class ConnectorsConfig(BaseModel):
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)
    slack: SlackConnectorConfig = Field(default_factory=SlackConnectorConfig)


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


def build_active_frame_store(config: BaseConfig) -> ActiveFrameStore:
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
        return PostgresActiveFrameStore(url, auto_migrate=auto_migrate)
    raise RuntimeError(f"Unsupported active Frame state backend: {backend}")


def build_history_store(config: BaseConfig) -> FrameHistoryStore:
    # No per-feature "disabled": "memory" is a test/dev override; otherwise ride
    # the shared frames.postgres, or an unavailable store (→ 503) when no DB.
    if config.frames.history.backend == "memory":
        return InMemoryFrameHistoryStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameHistoryStore(url, auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameHistoryStore()


def build_usage_store(config: BaseConfig) -> UsageStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503 on reads/events). No per-feature "disabled".
    if config.frames.usage.backend == "memory":
        return InMemoryUsageStore()
    url = config.frames.postgres.url
    if url:
        return PostgresUsageStore(url, auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableUsageStore()


def build_group_store(config: BaseConfig) -> FrameGroupStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503). No per-feature "disabled".
    if config.frames.groups.backend == "memory":
        return InMemoryFrameGroupStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameGroupStore(url, auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameGroupStore()


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


def build_task_store(config: BaseConfig) -> InMemoryTaskStore | PostgresTaskStore:
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
    return PostgresTaskStore(url, auto_migrate=config.tasks.auto_migrate or config.frames.postgres.auto_migrate)


def _keycloak_token_url(issuer_url: str) -> str:
    issuer_url = issuer_url.rstrip("/")
    if not issuer_url:
        return ""
    return f"{issuer_url}/protocol/openid-connect/token"
