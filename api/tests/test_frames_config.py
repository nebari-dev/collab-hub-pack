from __future__ import annotations

import pytest

from collab_hub_api.config import (
    Config,
    build_active_frame_store,
    build_group_store,
    build_history_store,
    build_task_store,
)
from collab_hub_api.frames.active_state import (
    DisabledActiveFrameStore,
    InMemoryActiveFrameStore,
    PostgresActiveFrameStore,
)
from collab_hub_api.frames.groups import (
    InMemoryFrameGroupStore,
    PostgresFrameGroupStore,
    UnavailableFrameGroupStore,
)
from collab_hub_api.frames.history import (
    InMemoryFrameHistoryStore,
    PostgresFrameHistoryStore,
    UnavailableFrameHistoryStore,
)
from collab_hub_api.tasks.store import InMemoryTaskStore, PostgresTaskStore


def test_build_active_frame_store_defaults_to_disabled_backend():
    store = build_active_frame_store(Config.parse())

    assert isinstance(store, DisabledActiveFrameStore)


def test_build_active_frame_store_supports_memory_backend():
    store = build_active_frame_store(Config.parse({"frames": {"active_state": {"backend": "memory"}}}))

    assert isinstance(store, InMemoryActiveFrameStore)


def test_active_state_postgres_falls_back_to_shared_url():
    # backend=postgres with no own URL uses the shared frames.postgres.url.
    config = Config.parse(
        {
            "frames": {
                "postgres": {"url": "postgresql://shared/db"},
                "active_state": {"backend": "postgres"},
            }
        }
    )
    store = build_active_frame_store(config)

    assert isinstance(store, PostgresActiveFrameStore)
    assert store.database_url == "postgresql://shared/db"


# --- History: no per-feature toggle, rides the shared frames.postgres --------


def test_build_history_store_unavailable_without_db():
    store = build_history_store(Config.parse())

    assert isinstance(store, UnavailableFrameHistoryStore)


def test_build_history_store_supports_memory_override():
    store = build_history_store(Config.parse({"frames": {"history": {"backend": "memory"}}}))

    assert isinstance(store, InMemoryFrameHistoryStore)


def test_build_history_store_uses_shared_postgres():
    store = build_history_store(Config.parse({"frames": {"postgres": {"url": "postgresql://shared/db"}}}))

    assert isinstance(store, PostgresFrameHistoryStore)


# --- Groups: same shared-postgres model as history ---------------------------


def test_build_group_store_unavailable_without_db():
    store = build_group_store(Config.parse())

    assert isinstance(store, UnavailableFrameGroupStore)


def test_build_group_store_supports_memory_override():
    store = build_group_store(Config.parse({"frames": {"groups": {"backend": "memory"}}}))

    assert isinstance(store, InMemoryFrameGroupStore)


def test_build_group_store_uses_shared_postgres():
    store = build_group_store(Config.parse({"frames": {"postgres": {"url": "postgresql://shared/db"}}}))

    assert isinstance(store, PostgresFrameGroupStore)


def test_build_task_store_supports_memory_backend():
    store = build_task_store(Config.parse({"tasks": {"backend": "memory"}}))

    assert isinstance(store, InMemoryTaskStore)


def test_build_task_store_requires_postgres_url_for_postgres_backend():
    with pytest.raises(RuntimeError, match="tasks.backend=postgres requires"):
        build_task_store(Config.parse({"tasks": {"backend": "postgres"}}))


def test_build_task_store_uses_shared_postgres_url():
    store = build_task_store(
        Config.parse({"tasks": {"backend": "postgres"}, "frames": {"postgres": {"url": "postgresql://shared/db"}}})
    )

    assert isinstance(store, PostgresTaskStore)
    assert store.url == "postgresql://shared/db"


def test_cors_config_preserves_desktop_auth_headers_and_credentials_toggle():
    config = Config.parse(
        {
            "security": {
                "cors": {
                    "allowed_origins": ["https://desktop.example.com"],
                    "allow_credentials": True,
                }
            }
        }
    )

    assert config.security.cors.allowed_headers == ["Authorization", "Content-Type"]
    assert config.security.cors.allow_credentials is True


def test_user_directory_config_defaults_to_disabled_keycloak():
    config = Config.parse()

    assert config.user_directory.enabled is False
    assert config.user_directory.provider == "keycloak"
    assert config.user_directory.keycloak.client_secret == ""


def test_user_directory_config_parses_nested_environment(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__USER_DIRECTORY__ENABLED", "true")
    monkeypatch.setenv("COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__ISSUER_URL", "https://keycloak.example/realms/hub")
    monkeypatch.setenv(
        "COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__TOKEN_URL",
        "https://keycloak.example/realms/hub/protocol/openid-connect/token",
    )
    monkeypatch.setenv(
        "COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__ADMIN_API_BASE_URL",
        "https://keycloak.example/admin/realms/hub",
    )
    monkeypatch.setenv("COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__CLIENT_ID", "nexus-user-directory")
    monkeypatch.setenv("COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__CLIENT_SECRET", "secret")

    config = Config()

    assert config.user_directory.enabled is True
    assert config.user_directory.keycloak.issuer_url == "https://keycloak.example/realms/hub"
    assert config.user_directory.keycloak.client_id == "nexus-user-directory"
    assert config.user_directory.keycloak.client_secret == "secret"
