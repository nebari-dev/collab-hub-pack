from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app


@pytest_asyncio.fixture
async def config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                # The HTTP MCP lifecycle is covered by test_mcp_http_mount_starts_session_manager.
                # These async API fixtures do not exercise /mcp and pytest-asyncio tears
                # generator fixtures down in a different task than MCP's anyio cancel scope.
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )


@pytest_asyncio.fixture
async def client(config: Config, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture
async def dev_client(config: Config, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    monkeypatch.setenv("DEV_AUTH_WORKSPACE", "default")
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture
async def cors_client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "security": {
                "cors": {
                    "allowed_origins": ["https://desktop.example.com"],
                    "allow_credentials": True,
                }
            },
            "frames": {
                "active_state": {"backend": "memory"},
                # See the default config fixture above.
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
