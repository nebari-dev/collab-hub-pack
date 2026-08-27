from __future__ import annotations

import pytest
from pydantic import ValidationError

from collab_hub_api.config import (
    Config,
    build_active_frame_store,
    build_group_store,
    build_history_store,
    build_postgres_pools,
    build_task_store,
    build_usage_store,
)
from collab_hub_api.frames.active_state import (
    DisabledActiveFrameStore,
    InMemoryActiveFrameStore,
    PostgresActiveFrameStore,
)
from collab_hub_api.frames.db import (
    DEFAULT_MAX_WAITING,
    MAX_WAITING_LIMIT,
    POOL_SIZE_LIMIT,
    TIMEOUT_SECONDS_LIMIT,
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


def _build(builder, config: Config):
    """Call a store builder the way make_app does: with a shared pool registry."""

    return builder(config, build_postgres_pools(config))


def test_build_active_frame_store_defaults_to_disabled_backend():
    store = _build(build_active_frame_store, Config.parse())

    assert isinstance(store, DisabledActiveFrameStore)


def test_build_active_frame_store_supports_memory_backend():
    store = _build(build_active_frame_store, Config.parse({"frames": {"active_state": {"backend": "memory"}}}))

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
    store = _build(build_active_frame_store, config)

    assert isinstance(store, PostgresActiveFrameStore)
    assert store.database_url == "postgresql://shared/db"


# --- History: no per-feature toggle, rides the shared frames.postgres --------


def test_build_history_store_unavailable_without_db():
    store = _build(build_history_store, Config.parse())

    assert isinstance(store, UnavailableFrameHistoryStore)


def test_build_history_store_supports_memory_override():
    store = _build(build_history_store, Config.parse({"frames": {"history": {"backend": "memory"}}}))

    assert isinstance(store, InMemoryFrameHistoryStore)


def test_build_history_store_uses_shared_postgres():
    store = _build(build_history_store, Config.parse({"frames": {"postgres": {"url": "postgresql://shared/db"}}}))

    assert isinstance(store, PostgresFrameHistoryStore)


# --- Groups: same shared-postgres model as history ---------------------------


def test_build_group_store_unavailable_without_db():
    store = _build(build_group_store, Config.parse())

    assert isinstance(store, UnavailableFrameGroupStore)


def test_build_group_store_supports_memory_override():
    store = _build(build_group_store, Config.parse({"frames": {"groups": {"backend": "memory"}}}))

    assert isinstance(store, InMemoryFrameGroupStore)


def test_build_group_store_uses_shared_postgres():
    store = _build(build_group_store, Config.parse({"frames": {"postgres": {"url": "postgresql://shared/db"}}}))

    assert isinstance(store, PostgresFrameGroupStore)


def test_build_task_store_supports_memory_backend():
    store = _build(build_task_store, Config.parse({"tasks": {"backend": "memory"}}))

    assert isinstance(store, InMemoryTaskStore)


def test_build_task_store_requires_postgres_url_for_postgres_backend():
    with pytest.raises(RuntimeError, match="tasks.backend=postgres requires"):
        _build(build_task_store, Config.parse({"tasks": {"backend": "postgres"}}))


def test_build_task_store_uses_shared_postgres_url():
    store = _build(
        build_task_store,
        Config.parse({"tasks": {"backend": "postgres"}, "frames": {"postgres": {"url": "postgresql://shared/db"}}}),
    )

    assert isinstance(store, PostgresTaskStore)
    assert store.url == "postgresql://shared/db"


def test_stores_on_the_shared_url_share_one_pool():
    # All relational stores riding frames.postgres must draw from a single
    # connection pool — that sharing is the point of issue #58.
    config = Config.parse(
        {
            "frames": {
                "postgres": {"url": "postgresql://shared/db"},
                "active_state": {"backend": "postgres"},
            },
            "tasks": {"backend": "postgres"},
        }
    )
    pools = build_postgres_pools(config)

    active = build_active_frame_store(config, pools)
    history = build_history_store(config, pools)
    groups = build_group_store(config, pools)
    usage = build_usage_store(config, pools)
    tasks = build_task_store(config, pools)

    assert active.db is history.db is groups.db is usage.db is tasks.db


def test_pool_config_parses_nested_environment(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MIN_SIZE", "2")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MAX_SIZE", "20")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MAX_WAITING", "5")

    config = Config()

    assert config.frames.postgres.pool.min_size == 2
    assert config.frames.postgres.pool.max_size == 20
    assert config.frames.postgres.pool.timeout_seconds == 1.5
    assert config.frames.postgres.pool.max_waiting == 5


def test_pool_config_defaults_bound_the_waiter_queue():
    # An unbounded waiter queue is what lets a saturated pool pile up requests.
    pool = Config.parse().frames.postgres.pool

    assert pool.max_waiting == DEFAULT_MAX_WAITING
    assert pool.max_waiting > 0


@pytest.mark.parametrize(
    ("pool", "expected"),
    [
        # The combination the helm schema cannot express: each field is valid
        # on its own, but the pool could never reach its own minimum.
        ({"min_size": 20, "max_size": 1}, "greater than or equal to min_size"),
        ({"max_size": 0}, "greater than or equal to 1"),
        ({"min_size": -1}, "greater than or equal to 0"),
        ({"timeout_seconds": 0}, "greater than 0"),
        ({"timeout_seconds": -1.0}, "greater than 0"),
        ({"timeout_seconds": TIMEOUT_SECONDS_LIMIT + 1}, "less than or equal to"),
        ({"max_size": POOL_SIZE_LIMIT + 1}, "less than or equal to"),
        ({"max_waiting": -1}, "greater than or equal to 0"),
        ({"max_waiting": MAX_WAITING_LIMIT + 1}, "less than or equal to"),
    ],
)
def test_invalid_pool_config_is_rejected(pool, expected):
    with pytest.raises(ValidationError, match=expected):
        Config.parse({"frames": {"postgres": {"url": "postgresql://shared/db", "pool": pool}}})


def test_invalid_pool_config_is_rejected_from_the_environment(monkeypatch):
    # Deployments configure the pool through helm-rendered env vars, so the
    # bounds have to hold on that path too, not just on dict parsing.
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MIN_SIZE", "20")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MAX_SIZE", "1")

    with pytest.raises(ValidationError, match="greater than or equal to min_size"):
        Config()


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


def test_cors_default_is_unchanged_for_deployments_that_do_not_set_it():
    # The restrictive value belongs to standalone exposure, which the chart
    # applies; changing the process default would take browser callers away
    # from existing deployments and from anyone running the image directly.
    assert Config.parse().security.cors.allowed_origins == ["*"]


def test_cors_rejects_wildcard_origin_with_credentials():
    # Starlette echoes the caller's Origin in that combination, which would let
    # any site make credentialed cross-origin calls.
    with pytest.raises(ValidationError, match="may not contain"):
        Config.parse({"security": {"cors": {"allowed_origins": ["*"], "allow_credentials": True}}})


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
