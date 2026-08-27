from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config, build_user_directory_client
from collab_hub_api.core import make_app
from collab_hub_api.dependencies import get_user_directory_client
from collab_hub_api.user_directory import (
    KeycloakUserDirectoryClient,
    UserDirectoryGroup,
    UserDirectoryUnavailableError,
    UserDirectoryUser,
)


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str, org: str = "org-a", workspace: str = "workspace-a") -> dict[str, str]:
    return {
        "IdToken-test": _jwt(
            {
                "preferred_username": user,
                "org_id": org,
                "workspace_id": workspace,
            }
        )
    }


async def _client(config: Config, monkeypatch, user_directory=None) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(config)
    if user_directory is not None:
        app.dependency_overrides[get_user_directory_client] = lambda: user_directory
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
        }
    )


class FakeUserDirectory:
    def __init__(self) -> None:
        self.user_query: tuple[str | None, int] | None = None
        self.group_query: tuple[str | None, int] | None = None

    def search_users(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryUser]:
        self.user_query = (query, limit)
        return [
            UserDirectoryUser(
                id="user-1",
                username="alice",
                email="alice@example.com",
                first_name="Alice",
                last_name="Ng",
            )
        ]

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]:
        self.group_query = (query, limit)
        return [UserDirectoryGroup(id="group-1", name="sales", path="/sales")]

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_user_directory_endpoints_require_auth(tmp_path, monkeypatch):
    async for client in _client(_config(tmp_path), monkeypatch):
        response = await client.get("/v1/user-directory/users")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_authenticated_users_can_search_user_directory(tmp_path, monkeypatch):
    directory = FakeUserDirectory()
    async for client in _client(_config(tmp_path), monkeypatch, directory):
        users = await client.get("/v1/user-directory/users?q=ali&limit=10", cookies=auth_cookie("bob"))
        groups = await client.get("/v1/user-directory/groups?q=sales&limit=5", cookies=auth_cookie("bob"))

    assert users.status_code == 200
    assert users.json() == [
        {
            "id": "user-1",
            "username": "alice",
            "email": "alice@example.com",
            "first_name": "Alice",
            "last_name": "Ng",
            "enabled": True,
        }
    ]
    assert directory.user_query == ("ali", 10)
    assert groups.status_code == 200
    assert groups.json() == [{"id": "group-1", "name": "sales", "path": "/sales"}]
    assert directory.group_query == ("sales", 5)


@pytest.mark.asyncio
async def test_user_directory_returns_unavailable_when_disabled(tmp_path, monkeypatch):
    async for client in _client(_config(tmp_path), monkeypatch):
        response = await client.get("/v1/user-directory/users", cookies=auth_cookie("alice"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "user_directory_unavailable"


def test_keycloak_user_directory_client_uses_service_account_credentials():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["client_credentials"]
            assert body["client_id"] == ["nexus-user-directory"]
            assert body["client_secret"] == ["secret"]
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        if request.url.path.endswith("/admin/realms/hub/users"):
            assert request.headers["Authorization"] == "Bearer service-token"
            assert request.url.params["search"] == "ali"
            assert request.url.params["max"] == "10"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "user-1",
                        "username": "alice",
                        "email": "alice@example.com",
                        "firstName": "Alice",
                        "lastName": "Ng",
                        "enabled": True,
                    }
                ],
            )
        if request.url.path.endswith("/admin/realms/hub/groups"):
            assert request.headers["Authorization"] == "Bearer service-token"
            assert request.url.params["search"] == "sales"
            assert request.url.params["max"] == "5"
            assert request.url.params["briefRepresentation"] == "true"
            return httpx.Response(200, json=[{"id": "group-1", "name": "sales", "path": "/sales"}])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = KeycloakUserDirectoryClient(
        token_url="https://keycloak.example/realms/hub/protocol/openid-connect/token",
        admin_api_base_url="https://keycloak.example/admin/realms/hub",
        client_id="nexus-user-directory",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )

    users = client.search_users("ali", limit=10)
    groups = client.search_groups("sales", limit=5)
    client.close()

    assert users == [
        UserDirectoryUser(
            id="user-1",
            username="alice",
            email="alice@example.com",
            first_name="Alice",
            last_name="Ng",
        )
    ]
    assert groups == [UserDirectoryGroup(id="group-1", name="sales", path="/sales")]
    assert [request.method for request in seen] == ["POST", "GET", "GET"]


def test_keycloak_user_directory_client_retries_once_after_stale_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            token = "stale-token" if len([r for r in seen if r.url.path.endswith("/token")]) == 1 else "fresh-token"
            return httpx.Response(200, json={"access_token": token, "expires_in": 300})
        if request.url.path.endswith("/admin/realms/hub/users"):
            if request.headers["Authorization"] == "Bearer stale-token":
                return httpx.Response(401, json={"error": "invalid_token"})
            assert request.headers["Authorization"] == "Bearer fresh-token"
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = KeycloakUserDirectoryClient(
        token_url="https://keycloak.example/realms/hub/protocol/openid-connect/token",
        admin_api_base_url="https://keycloak.example/admin/realms/hub",
        client_id="nexus-user-directory",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.search_users() == []
    client.close()

    assert [request.method for request in seen] == ["POST", "GET", "POST", "GET"]


def test_keycloak_user_directory_client_reports_keycloak_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        if request.url.path.endswith("/admin/realms/hub/users"):
            return httpx.Response(403, json={"error": "forbidden"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = KeycloakUserDirectoryClient(
        token_url="https://keycloak.example/realms/hub/protocol/openid-connect/token",
        admin_api_base_url="https://keycloak.example/admin/realms/hub",
        client_id="nexus-user-directory",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UserDirectoryUnavailableError, match="HTTP 403"):
        client.search_users()
    client.close()


def test_user_directory_token_url_defaults_from_issuer_url():
    config = Config.parse(
        {
            "user_directory": {
                "enabled": True,
                "keycloak": {
                    "issuer_url": "https://keycloak.example/realms/hub/",
                    "admin_api_base_url": "https://keycloak.example/admin/realms/hub",
                    "client_id": "nexus-user-directory",
                    "client_secret": "secret",
                },
            }
        }
    )

    client = build_user_directory_client(config)
    assert isinstance(client, KeycloakUserDirectoryClient)
    assert client.token_url == "https://keycloak.example/realms/hub/protocol/openid-connect/token"
    client.close()
