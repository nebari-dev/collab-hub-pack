from __future__ import annotations

import base64
import json

import httpx
from httpx import ASGITransport, AsyncClient, Response

from collab_hub_api.config import Config
from collab_hub_api.connectors.gmail_client import _message_content_info, _recipient_headers
from collab_hub_api.core import make_app


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _auth_header() -> dict[str, str]:
    token = _jwt(
        {
            "preferred_username": "alice",
            "org_id": "org-a",
            "workspace_id": "workspace-a",
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "connectors": {
                "google": {
                    "static_access_token": "google-token-alice",
                    "drive_api_base_url": "https://google.test/drive/v3",
                    "gmail_api_base_url": "https://google.test/gmail/v1",
                }
            },
        }
    )


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


async def test_gmail_status_search_and_read_are_live_read_only_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    methods: list[str] = []

    def handler(request: httpx.Request) -> Response:
        methods.append(request.method)
        path = request.url.path
        if path.endswith("/users/me/messages"):
            if request.url.params.get("maxResults") == "1":
                assert request.url.params["q"] == "newer_than:1d"
                return Response(200, json={"messages": []})
            assert request.url.params["pageToken"] == "gmail-page-2"
            return Response(
                200,
                json={
                    "messages": [{"id": "msg-1", "threadId": "thread-1"}],
                    "resultSizeEstimate": 1,
                },
            )
        if path.endswith("/users/me/messages/msg-1") and request.url.params.get("format") == "metadata":
            return Response(
                200,
                json={
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "snippet": "Review https://unsafe.example.test/plan",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Connector plan"},
                            {"name": "From", "value": "Mark <mark@example.com>"},
                            {"name": "To", "value": "alice@example.com"},
                        ]
                    },
                },
            )
        if path.endswith("/users/me/messages/msg-1") and request.url.params.get("format") == "full":
            return Response(
                200,
                json={
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "payload": {
                        "headers": [{"name": "Subject", "value": "Connector plan"}],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "filename": "",
                                "body": {
                                    "data": _encoded("The rollout is approved. See https://unsafe.example.test/task")
                                },
                            },
                            {
                                "mimeType": "application/pdf",
                                "filename": "private.pdf",
                                "body": {"data": _encoded("PRIVATE ATTACHMENT CONTENT")},
                            },
                        ],
                    },
                },
            )
        return Response(404, json={"error": {"message": "not found"}})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = await client.get("/v1/connectors/gmail/status", headers=_auth_header())
            search = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "connector", "page_token": "gmail-page-2"},
            )
            read = await client.post(
                "/v1/connectors/gmail/messages/msg-1/read",
                headers=_auth_header(),
                json={"max_chars": 1000},
            )

    assert status.json()["state"] == "connected"
    assert search.status_code == 200
    assert search.json()["messages"][0]["snippet"] == "Review [link]"
    assert search.json()["content_trust"] == "external_untrusted"
    assert read.status_code == 200
    assert read.json()["text"] == "The rollout is approved. See [link]"
    assert read.json()["body_format"] == "plain_text"
    assert read.json()["has_attachments"] is True
    assert read.json()["attachment_count"] == 1
    assert "PRIVATE ATTACHMENT CONTENT" not in read.text
    assert set(methods) == {"GET"}


async def test_gmail_filter_only_dates_use_the_requested_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    seen_queries: list[str | None] = []

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/users/me/messages"):
            seen_queries.append(request.url.params.get("q"))
            return Response(200, json={"messages": []})
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            by_label = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "", "label_ids": ["INBOX"]},
            )
            by_date = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={
                    "query": "",
                    "since_date": "2026-07-09",
                    "until_date": "2026-07-09",
                    "time_zone": "America/New_York",
                },
            )
            unbounded = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": ""},
            )
            blank_labels = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "", "label_ids": [" ", "\t"]},
            )

    assert by_label.status_code == 200
    assert by_date.status_code == 200
    assert unbounded.status_code == 422
    assert blank_labels.status_code == 422
    assert seen_queries == [None, "after:1783569600 before:1783656000"]


def test_gmail_openapi_and_recipient_contract(tmp_path):
    schemas = make_app(_config(tmp_path)).openapi()["components"]["schemas"]
    assert "page_token" in schemas["GmailSearchRequest"]["properties"]
    assert "next_page_token" in schemas["GmailSearchResponse"]["properties"]
    assert "time_zone" in schemas["GmailSearchRequest"]["properties"]
    assert "body_format" in schemas["GmailReadResponse"]["properties"]
    assert "attachment_count" in schemas["GmailReadResponse"]["properties"]
    assert _recipient_headers(
        {
            "to": '"Doe, Jane" <jane@example.com>, John <john@example.com>',
            "cc": "Team <team@example.com>",
        }
    ) == [
        '"Doe, Jane" <jane@example.com>',
        "John <john@example.com>",
        "Team <team@example.com>",
    ]


def test_gmail_content_info_distinguishes_multipart_bodies_and_attachments():
    assert _message_content_info(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": "plain"}},
                {"mimeType": "text/html", "filename": "", "body": {"data": "html"}},
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "attachment-1"},
                },
            ],
        }
    ) == ("multipart", 1)
