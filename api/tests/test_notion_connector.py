from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors.models import (
    NOTION_READONLY_CAPABILITIES,
    NotionDatabaseQueryRequest,
    NotionPageReadRequest,
    NotionSearchRequest,
)
from collab_hub_api.core import make_app

STATIC_TOKEN = "secret_notion-bot-token-alice"
PAGE_ID = "0123456789abcdef0123456789abcdef"
DATABASE_ID = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _allow_unsigned_bearer(monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _auth_header() -> dict[str, str]:
    token = _jwt({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"})
    return {"Authorization": f"Bearer {token}"}


def _config(tmp_path, **notion) -> Config:
    notion_config = {
        "static_access_token": STATIC_TOKEN,
        "api_base_url": "https://notion.test",
        "notion_version": "2022-06-28",
        **notion,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {"notion": notion_config},
        }
    )


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


def _title_property(text: str, *, href: str | None = None) -> dict:
    return {"Name": {"type": "title", "title": [{"plain_text": text, "href": href}]}}


def _page_hit(page_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": "https://www.notion.so/should-be-dropped",
        "last_edited_time": last_edited,
        "properties": _title_property(title),
    }


def _database_hit(database_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "database",
        "id": database_id,
        "url": "https://www.notion.so/db-dropped",
        "last_edited_time": last_edited,
        "title": [{"plain_text": title, "href": None}],
    }


def _paragraph(text: str, *, href: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [{"plain_text": text, "href": href}]},
    }


def _ok_status_handler(request: httpx.Request) -> Response:
    path = request.url.path
    if path.endswith("/v1/users/me"):
        assert request.headers.get("Notion-Version") == "2022-06-28"
        return Response(200, json={"object": "user", "type": "bot", "bot": {"workspace_name": "Acme HQ"}})
    if path.endswith("/v1/search"):
        return Response(200, json={"object": "list", "results": [], "has_more": False, "next_cursor": None})
    return Response(404, json={"object": "error", "status": 404, "code": "not_found", "message": "Not Found"})


# --- request model bounds -------------------------------------------------


def test_notion_search_request_bounds() -> None:
    request = NotionSearchRequest(query="notes", limit=100, time_zone="America/New_York")
    assert request.limit == 100
    assert request.time_zone == "America/New_York"
    with pytest.raises(ValidationError):
        NotionSearchRequest(limit=101)
    with pytest.raises(ValidationError):
        NotionSearchRequest(time_zone="Mars/Phobos")
    with pytest.raises(ValidationError):
        NotionSearchRequest(since_date="2026-02-01", until_date="2026-01-01")


def test_notion_read_and_query_request_bounds() -> None:
    assert NotionPageReadRequest().max_chars == 20_000
    with pytest.raises(ValidationError):
        NotionPageReadRequest(max_chars=0)
    with pytest.raises(ValidationError):
        NotionDatabaseQueryRequest(limit=0)


# --- status ---------------------------------------------------------------


async def test_notion_status_connected_reports_workspace(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "Acme HQ"
    assert body["scopes"] == NOTION_READONLY_CAPABILITIES
    assert STATIC_TOKEN not in response.text


async def test_notion_status_not_connected_without_token_source(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path, static_access_token="", broker_token_url=""))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "not_connected"


async def test_notion_status_reconnect_when_token_rejected(tmp_path, monkeypatch):
    # A token that brokers but cannot read (401 on the capability probe) must
    # report reconnect_required, not connected (contract 4).
    def handler(request: httpx.Request) -> Response:
        return Response(401, json={"object": "error", "status": 401, "code": "unauthorized", "message": "bad token"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "reconnect_required"


async def test_notion_status_unavailable_on_server_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/v1/users/me"):
            return Response(500, json={"object": "error", "status": 500, "code": "internal", "message": "boom"})
        return _ok_status_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "unavailable"


# --- config permutations --------------------------------------------------


async def test_notion_static_token_wins_over_broker(tmp_path, monkeypatch):
    # When both a static token and a broker URL are configured the static token
    # is used and the broker is never called. This is intentional (dev/CI).
    def handler(request: httpx.Request) -> Response:
        assert "broker" not in request.url.path, "static token must short-circuit the broker"
        assert request.headers.get("Authorization") == f"Bearer {STATIC_TOKEN}"
        return _ok_status_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, broker_token_url="https://keycloak.test/broker/token"))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    assert response.json()["state"] == "connected"


# --- search ---------------------------------------------------------------


async def test_notion_search_returns_hits_and_drops_urls(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/v1/search"):
            body = json.loads(request.content)
            assert body["sort"] == {"timestamp": "last_edited_time", "direction": "descending"}
            return Response(
                200,
                json={
                    "results": [
                        _page_hit(PAGE_ID, "Roadmap", "2026-02-01T00:00:00.000Z"),
                        _database_hit(DATABASE_ID, "Tasks", "2026-01-15T00:00:00.000Z"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search", headers=_auth_header(), json={"query": "roadmap"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert [hit["title"] for hit in body["hits"]] == ["Roadmap", "Tasks"]
    assert [hit["object"] for hit in body["hits"]] == ["page", "database"]
    # No URL field leaks through anywhere in the response.
    assert "url" not in response.text
    assert "notion.so" not in response.text


async def test_notion_search_object_type_filter_and_page_token(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(
            200,
            json={
                "results": [_page_hit(PAGE_ID, "Only pages", "2026-02-01T00:00:00.000Z")],
                "has_more": True,
                "next_cursor": "cursor-2",
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "pages", "object_type": "page", "limit": 1, "page_token": "cursor-1"},
        )
    body = response.json()
    assert captured["body"]["filter"] == {"value": "page", "property": "object"}
    assert captured["body"]["start_cursor"] == "cursor-1"
    # limit=1 reached with has_more -> echo the provider cursor to continue.
    assert body["next_page_token"] == "cursor-2"


async def test_notion_search_stops_paging_at_lower_bound(tmp_path, monkeypatch):
    # Newest-first sort + a since_date lower bound: the client must stop paging
    # as soon as it sees an item older than the bound and not fetch further pages.
    pages_served: list[int] = []

    def handler(request: httpx.Request) -> Response:
        body = json.loads(request.content)
        cursor = body.get("start_cursor")
        pages_served.append(1)
        if cursor is None:
            return Response(
                200,
                json={
                    "results": [
                        _page_hit(PAGE_ID, "Recent", "2026-02-10T00:00:00.000Z"),
                        _page_hit("11111111111111111111111111111111", "Too old", "2026-01-01T00:00:00.000Z"),
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                },
            )
        raise AssertionError("client should have stopped before requesting a second page")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "since_date": "2026-02-01", "time_zone": "UTC"},
        )
    body = response.json()
    assert [hit["title"] for hit in body["hits"]] == ["Recent"]
    assert body["next_page_token"] == ""
    assert len(pages_served) == 1


async def test_notion_search_stale_cursor_is_422(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            400,
            json={"object": "error", "status": 400, "code": "validation_error", "message": "invalid start_cursor"},
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "page_token": "garbage"},
        )
    assert response.status_code == 422
    assert STATIC_TOKEN not in response.text


# --- read page ------------------------------------------------------------


async def test_notion_read_page_assembles_text_and_drops_href(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(
                200,
                json={"object": "page", "id": PAGE_ID, "properties": _title_property("Design Notes")},
            )
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            return Response(
                200,
                json={
                    "results": [
                        _paragraph("First line with a ", href="https://evil.example/link"),
                        _paragraph("Second line"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Design Notes"
    assert "First line with a" in body["text"]
    assert "Second line" in body["text"]
    assert body["truncated"] is False
    # href never leaks into assembled text.
    assert "evil.example" not in response.text


async def test_notion_read_page_truncates_and_flags_more(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(200, json={"object": "page", "id": PAGE_ID, "properties": _title_property("Big")})
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            return Response(
                200,
                json={"results": [_paragraph("x" * 50)], "has_more": False, "next_cursor": None},
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/pages/{PAGE_ID}/read",
            headers=_auth_header(),
            json={"max_chars": 10},
        )
    body = response.json()
    assert len(body["text"]) == 10
    assert body["truncated"] is True


async def test_notion_read_page_rejects_bad_id(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/pages/not-a-real-id/read", headers=_auth_header(), json={}
        )
    assert response.status_code == 422


# --- query database -------------------------------------------------------


async def test_notion_query_database_emits_native_timestamp_filter(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith(f"/v1/databases/{DATABASE_ID}/query"):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json={
                    "results": [_page_hit(PAGE_ID, "Row 1", "2026-02-05T00:00:00.000Z")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query",
            headers=_auth_header(),
            json={"since_date": "2026-02-01", "until_date": "2026-02-28", "time_zone": "UTC"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [row["title"] for row in body["rows"]] == ["Row 1"]
    # Unlike search, the window is sent to the provider as a native filter.
    filt = captured["body"]["filter"]
    assert filt["timestamp"] == "last_edited_time"
    assert "on_or_after" in filt["last_edited_time"]
    assert "on_or_before" in filt["last_edited_time"]
    assert STATIC_TOKEN not in response.text


async def test_notion_query_database_rejects_bad_id(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/databases/bad/query", headers=_auth_header(), json={}
        )
    assert response.status_code == 422


async def test_notion_search_provider_timeout_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"}
        )
    assert response.status_code == 502
