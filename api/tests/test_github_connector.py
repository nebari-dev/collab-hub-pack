from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors.github_client import _project_filter_value
from collab_hub_api.connectors.models import GITHUB_READONLY_SCOPES, GitHubSearchRequest
from collab_hub_api.core import make_app

STATIC_TOKEN = "gho_github-token-alice-secret"


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


def _config(tmp_path, **github) -> Config:
    github_config = {
        "static_access_token": STATIC_TOKEN,
        "api_base_url": "https://github.test/api",
        **github,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {"github": github_config},
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


def _ok_status_handler(request: httpx.Request) -> Response:
    path = request.url.path
    if path.endswith("/user"):
        return Response(
            200,
            json={"login": "octocat", "id": 1},
            headers={"X-OAuth-Scopes": "repo, read:org, read:project, user:email"},
        )
    if path.endswith("/user/repos"):
        return Response(200, json=[{"full_name": "acme/widgets"}])
    return Response(404, json={"message": "Not Found"})


def _issue_item(number: int, *, body: str = "", title: str = "Example") -> dict:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": "octocat"},
        "assignees": [{"login": "assignee1"}],
        "labels": [{"name": "bug"}],
        "comments": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "repository_url": "https://github.test/api/repos/acme/widgets",
        "body": body,
    }


# --- request model bounds -------------------------------------------------


def test_github_search_request_bounds() -> None:
    request = GitHubSearchRequest(query="bug", limit=25, repo="acme/widgets")
    assert request.limit == 25
    with pytest.raises(ValidationError):
        GitHubSearchRequest(query="", limit=1)
    with pytest.raises(ValidationError):
        GitHubSearchRequest(query="bug", limit=26)


# --- status ---------------------------------------------------------------


async def test_github_status_connected_reports_account(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "octocat"
    assert set(body["scopes"]) == set(GITHUB_READONLY_SCOPES)


async def test_github_status_reports_actual_token_scopes(tmp_path, monkeypatch):
    # The status must report the token's real X-OAuth-Scopes grant, not the
    # configured intention: a stale link missing read:project shows as missing.
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/user"):
            return Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"})
        if path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is True
    assert body["scopes"] == ["repo", "read:org"]
    assert "read:project" not in body["scopes"]


async def test_github_status_not_connected_without_token_source(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path, static_access_token="", broker_token_url=""))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "not_connected"


async def test_github_status_reconnect_for_rejected_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(401, json={"message": "Bad credentials"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "reconnect_required"


# --- search ---------------------------------------------------------------


async def test_github_search_returns_hits_and_applies_repo_qualifier(tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            seen["q"] = request.url.params.get("q", "")
            return Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [_issue_item(7, title="Login bug", body="steps to repro")],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "login", "limit": 10, "repo": "acme/widgets"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["number"] == 7
    assert hit["repo"] == "acme/widgets"
    assert hit["title"] == "Login bug"
    assert hit["assignees"] == ["assignee1"]
    assert hit["labels"] == ["bug"]
    assert body["next_page_token"] == ""  # single partial page => no continuation
    assert "repo:acme/widgets" in seen["q"]


async def test_github_search_paginates_and_rejects_stale_page_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            page = request.url.params.get("page", "1")
            return Response(
                200,
                json={
                    "total_count": 5,
                    "incomplete_results": False,
                    "items": [_issue_item(int(page))],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        first = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "limit": 1},
        )
        token = first.json()["next_page_token"]
        assert token, "a full page under the result cap must yield a continuation token"

        cont = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "limit": 1, "page_token": token},
        )
        assert cont.status_code == 200

        stale = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "different", "limit": 1, "page_token": token},
        )
    assert stale.status_code == 422


async def test_github_search_surfaces_incomplete_results(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(200, json={"total_count": 1, "incomplete_results": True, "items": [_issue_item(1)]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"}
        )
    assert response.json()["incomplete_results"] is True


async def test_github_search_honors_rate_limit_retry_after(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(
                429,
                headers={"Retry-After": "42", "x-ratelimit-remaining": "0"},
                json={"message": "API rate limit exceeded"},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"}
        )
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "42"


async def test_github_search_sanitizes_link_text(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [_issue_item(1, title="See http://evil.test/x", body="ping http://evil.test")],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"}
        )
    assert "http://evil.test" not in response.text


async def test_github_endpoints_do_not_expose_access_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/user"):
            return Response(200, json={"login": "octocat"})
        if request.url.path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        if request.url.path.endswith("/search/issues"):
            return Response(200, json={"total_count": 1, "incomplete_results": False, "items": [_issue_item(1)]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        status_body = (await client.get("/v1/connectors/github/status", headers=_auth_header())).text
        search_body = (
            await client.post("/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"})
        ).text
    assert STATIC_TOKEN not in status_body
    assert STATIC_TOKEN not in search_body


# --- item read ------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_github_item_read_returns_bounded_body_and_comments(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "Login bug",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "assignees": [{"login": "octocat"}, {"login": "hubot"}],
                    "labels": [{"name": "bug"}, {"name": "priority:high"}],
                    "comments": 1,
                    "body": "The login flow breaks.",
                    "pull_request": {"url": "x"},
                },
            )
        if path.endswith("/repos/acme/widgets/issues/7/comments"):
            return Response(200, json=[{"user": {"login": "hubot"}, "body": "I can repro this."}])
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=[{"user": {"login": "hubot"}, "state": "APPROVED"}])
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": [{"login": "reviewer1"}]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["item"]["number"] == 7
    assert body["item"]["is_pull_request"] is True
    assert body["item"]["assignees"] == ["octocat", "hubot"]
    assert body["item"]["labels"] == ["bug", "priority:high"]
    # PR-only fields sourced from the pulls endpoint, not the issues endpoint.
    assert body["item"]["requested_reviewers"] == ["reviewer1"]
    assert body["item"]["reviews"] == [{"user": "hubot", "state": "APPROVED", "body": ""}]
    assert "login flow breaks" in body["text"]
    assert "repro this" in body["text"]
    assert body["truncated"] is False


async def test_github_item_read_pr_draft_and_merge_state(tmp_path, monkeypatch):
    # is_draft and merge_state come from the pulls payload already fetched for
    # requested_reviewers; state="closed" alone can't tell merged from abandoned.
    pulls = {
        1: {"draft": True, "state": "open", "merged": False},
        2: {"draft": False, "state": "closed", "merged": True},
        3: {"draft": False, "state": "closed", "merged": False},
        4: {"draft": False, "state": "open", "merged": False},
    }

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        for n in pulls:
            if path.endswith(f"/repos/acme/widgets/issues/{n}"):
                return Response(
                    200,
                    json={
                        "number": n,
                        "title": "A PR",
                        "state": pulls[n]["state"],
                        "user": {"login": "octocat"},
                        "comments": 0,
                        "body": "b",
                        "pull_request": {"url": "x"},
                    },
                )
            if path.endswith(f"/repos/acme/widgets/pulls/{n}/reviews"):
                return Response(200, json=[])
            if path.endswith(f"/repos/acme/widgets/pulls/{n}"):
                return Response(200, json={"requested_reviewers": [], **pulls[n]})
        if path.endswith("/repos/acme/widgets/issues/8"):
            return Response(
                200,
                json={"number": 8, "title": "An issue", "state": "open",
                      "user": {"login": "octocat"}, "comments": 0, "body": "b"},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    expected = {1: (True, "open"), 2: (False, "merged"), 3: (False, "closed_unmerged"), 4: (False, "open")}
    async with _client(app) as client:
        for n, (want_draft, want_merge) in expected.items():
            response = await client.post(
                f"/v1/connectors/github/items/{n}/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets", "max_chars": 5000},
            )
            item = response.json()["item"]
            assert item["is_draft"] is want_draft, n
            assert item["merge_state"] == want_merge, n
        # A plain issue leaves both fields defaulted (never fetches pulls).
        response = await client.post(
            "/v1/connectors/github/items/8/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
        item = response.json()["item"]
        assert item["is_draft"] is False
        assert item["merge_state"] == ""


async def test_github_item_read_fetches_most_recent_comments(tmp_path, monkeypatch):
    # Comments come back oldest-first; with 65 comments over 30/page the most
    # recent ones are on page 3. We must request the last page, not page 1.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7/comments"):
            seen["page"] = request.url.params.get("page")
            seen["per_page"] = request.url.params.get("per_page")
            return Response(200, json=[{"user": {"login": "hubot"}, "body": "the latest comment"}])
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "comments": 65, "body": "b"})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    assert response.status_code == 200
    assert seen["per_page"] == "30"
    assert seen["page"] == "3"  # ceil(65 / 30)
    assert "the latest comment" in response.json()["text"]


async def test_github_item_read_pr_reviews_keep_newest_tail(tmp_path, monkeypatch):
    # Reviews are oldest-first with no sort option; an early CHANGES_REQUESTED
    # must not hide the later APPROVED. per_page=100 + newest tail keeps it.
    reviews = [{"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"}]
    reviews += [{"user": {"login": f"u{i}"}, "state": "COMMENTED"} for i in range(40)]
    reviews += [{"user": {"login": "alice"}, "state": "APPROVED"}]  # newest, last
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            seen["per_page"] = request.url.params.get("per_page")
            return Response(200, json=reviews)
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": []})
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "x",
                    "state": "open",
                    "comments": 0,
                    "body": "b",
                    "pull_request": {"url": "x"},
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets"},
        )
    returned = response.json()["item"]["reviews"]
    assert seen["per_page"] == "100"
    assert len(returned) <= 30
    # The newest review is kept (last); the early stale state fell off the tail.
    assert returned[-1] == {"user": "alice", "state": "APPROVED", "body": ""}


async def test_github_item_read_pr_review_body(tmp_path, monkeypatch):
    # The reviewer's rationale is returned, link-sanitized, and length-capped so a
    # long review can't blow the response, while an empty body stays empty.
    reviews = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED", "body": "Fix the null check http://evil.test/x"},
        {"user": {"login": "bob"}, "state": "APPROVED", "body": "y" * 900},
        {"user": {"login": "carol"}, "state": "COMMENTED", "body": ""},
    ]

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "PR", "state": "open",
                                       "user": {"login": "octocat"}, "comments": 0, "body": "b",
                                       "pull_request": {"url": "x"}})
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=reviews)
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": []})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    out = response.json()["item"]["reviews"]
    assert out[0]["body"].startswith("Fix the null check")
    assert "http://" not in out[0]["body"]  # link-sanitized, no raw URL leak
    assert len(out[1]["body"]) <= 501 and out[1]["body"].endswith("…")  # capped
    assert out[2]["body"] == ""


async def test_github_item_read_pr_requested_teams(tmp_path, monkeypatch):
    # A team requested for review is surfaced alongside individual reviewers, so a
    # reviewer-grouping report doesn't silently drop team requests.
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "PR", "state": "open",
                                       "user": {"login": "octocat"}, "comments": 0, "body": "b",
                                       "pull_request": {"url": "x"}})
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=[])
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={
                "requested_reviewers": [{"login": "alice"}],
                "requested_teams": [{"name": "Platform", "slug": "platform"},
                                    {"name": "Security", "slug": "security"}],
            })
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    item = response.json()["item"]
    assert item["requested_reviewers"] == ["alice"]
    assert item["requested_teams"] == ["Platform", "Security"]


async def test_github_item_read_truncates_to_max_chars(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "body": "A" * 5000})
        if path.endswith("/comments"):
            return Response(200, json=[])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 100},
        )
    body = response.json()
    assert body["truncated"] is True
    assert len(body["text"]) <= 101  # max_chars plus the ellipsis


async def test_github_item_read_rejects_bad_repo(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "not-a-repo", "max_chars": 100},
        )
    assert response.status_code == 422


# --- file read ------------------------------------------------------------


async def test_github_file_read_decodes_base64(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            assert request.url.params.get("ref") == "main"
            return Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "size": 11,
                    "content": _b64(b"hello world"),
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "src/app.py", "ref": "main", "max_chars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "hello world"
    assert body["binary"] is False
    assert body["too_large"] is False


async def test_github_file_read_percent_encodes_path(tmp_path, monkeypatch):
    # '?' and spaces are legal in git paths and pass _validate_github_path; they
    # must be percent-encoded so the remainder cannot leak into the query string
    # and override ref.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            seen["raw_path"] = request.url.raw_path.decode()
            seen["ref"] = request.url.params.get("ref")
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": 2, "content": _b64(b"hi")},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "docs/a b?ref=evil", "ref": "main", "max_chars": 5000},
        )
    assert response.status_code == 200
    # The '?' and space stay inside the encoded path; ref is the caller's value, not "evil".
    assert "docs/a%20b%3Fref%3Devil" in seen["raw_path"]
    assert seen["ref"] == "main"


async def test_github_file_read_reports_too_large(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(200, json={"type": "file", "encoding": "none", "size": 2_000_000, "content": ""})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "big.bin", "max_chars": 5000},
        )
    body = response.json()
    assert body["too_large"] is True
    assert body["content"] == ""


async def test_github_file_read_flags_binary(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": 4, "content": _b64(b"\xff\xfe\x00\x01")},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "logo.png", "max_chars": 5000},
        )
    body = response.json()
    assert body["binary"] is True
    assert body["content"] == ""


async def test_github_file_read_flags_lfs_pointer(tmp_path, monkeypatch):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12345\n"

    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": len(pointer), "content": _b64(pointer)},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "model.bin", "max_chars": 5000},
        )
    body = response.json()
    assert "lfs" in body["unsupported_reason"].lower()
    assert body["content"] == ""


async def test_github_file_read_rejects_path_and_ref_traversal(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        bad_path = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "../../etc/passwd", "max_chars": 100},
        )
        bad_ref = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "src/app.py", "ref": "../secrets", "max_chars": 100},
        )
        abs_path = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "/etc/passwd", "max_chars": 100},
        )
    assert bad_path.status_code == 422
    assert bad_ref.status_code == 422
    assert abs_path.status_code == 422


async def test_github_reads_do_not_expose_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "body": "b"})
        if path.endswith("/comments"):
            return Response(200, json=[])
        if "/contents/" in path:
            return Response(200, json={"type": "file", "encoding": "base64", "size": 1, "content": _b64(b"x")})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        item_body = (
            await client.post(
                "/v1/connectors/github/items/7/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets"},
            )
        ).text
        file_body = (
            await client.post(
                "/v1/connectors/github/files/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets", "path": "a.txt"},
            )
        ).text
    assert STATIC_TOKEN not in item_body
    assert STATIC_TOKEN not in file_body


# --- project boards (Projects V2 / GraphQL) -------------------------------


def _graphql_handler(*, org=None, user=None):
    """A mock that answers POST /graphql with the given organization/user data."""

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/graphql"):
            return Response(200, json={"data": {"organization": org, "user": user}})
        return Response(404, json={"message": "Not Found"})

    return handler


def _list_node():
    return {
        "projectsV2": {
            "nodes": [
                {
                    "number": 1,
                    "title": "Roadmap",
                    "shortDescription": "Q3 plan",
                    "closed": False,
                    "items": {"totalCount": 12},
                }
            ]
        }
    }


def _read_node():
    return {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 2,
                "nodes": [
                    {
                        "type": "PULL_REQUEST",
                        "content": {
                            "__typename": "PullRequest",
                            "number": 42,
                            "title": "Add feature",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {
                            "nodes": [
                                {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "In Progress",
                                    "field": {"name": "Status"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "DRAFT_ISSUE",
                        "content": {"__typename": "DraftIssue", "title": "An idea"},
                        "fieldValues": {"nodes": []},
                    },
                ]
            },
        }
    }


async def test_github_list_projects_org(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_list_node(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["number"] == 1
    assert projects[0]["title"] == "Roadmap"
    assert projects[0]["items_count"] == 12


async def test_github_list_projects_falls_back_to_user(tmp_path, monkeypatch):
    # A personal owner: organization(login) resolves null, user(login) has the boards.
    _install_mock_client(monkeypatch, _graphql_handler(org=None, user=_list_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "mcshayla"}
        )
    assert response.status_code == 200
    assert len(response.json()["projects"]) == 1


async def test_github_read_project_items_link_prs_for_chaining(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai", "max_items": 50},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["project"]["title"] == "Roadmap"
    items = body["items"]
    assert len(items) == 2
    pr = items[0]
    assert pr["type"] == "PULL_REQUEST"
    assert pr["status"] == "In Progress"
    assert pr["repo"] == "acme/widgets"  # so the model can chain read_github_item
    assert pr["number"] == 42
    draft = items[1]
    assert draft["type"] == "DRAFT_ISSUE"
    assert draft["repo"] == "" and draft["number"] == 0
    # Full board fit in the response -> not truncated.
    assert body["total_count"] == 2
    assert body["truncated"] is False


async def test_github_read_project_flags_truncation(tmp_path, monkeypatch):
    # Board reports 91 items but only a page came back -> truncated=True and
    # total_count reflects the real size, so the caller knows it's partial.
    node = _read_node()
    node["projectV2"]["items"]["totalCount"] = 91
    _install_mock_client(monkeypatch, _graphql_handler(org=node, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    body = response.json()
    assert body["total_count"] == 91
    assert body["truncated"] is True


async def test_github_read_project_auto_paginates(tmp_path, monkeypatch):
    # The read walks pages server-side (GitHub caps a page at 100). Two pages
    # here -> both accumulated into one response, not truncated.
    def _page(nodes, has_next, end_cursor):
        return {
            "projectV2": {
                "number": 1,
                "title": "Roadmap",
                "shortDescription": "",
                "closed": False,
                "items": {
                    "totalCount": 3,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "nodes": nodes,
                },
            }
        }

    def item(n):
        return {"type": "ISSUE", "content": {"__typename": "Issue", "number": n, "title": f"i{n}",
                "repository": {"nameWithOwner": "acme/widgets"}}, "fieldValues": {"nodes": []}}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/graphql"):
            variables = json.loads(request.content.decode("utf-8")).get("variables", {})
            if not variables.get("after"):
                page1 = _page([item(1), item(2)], True, "CURSOR2")
                return Response(200, json={"data": {"organization": page1, "user": None}})
            page2 = _page([item(3)], False, None)
            return Response(200, json={"data": {"organization": page2, "user": None}})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    body = response.json()
    assert [i["number"] for i in body["items"]] == [1, 2, 3]  # both pages accumulated
    assert body["total_count"] == 3
    assert body["truncated"] is False


async def test_github_project_owner_not_found(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=None, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "ghost-org"}
        )
    assert response.status_code == 404


async def test_github_projects_reject_bad_owner(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_list_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "bad/owner"}
        )
    assert response.status_code == 422


def _read_node_states():
    """A board whose items exercise state (open/closed) and a REDACTED node
    (content the viewer cannot access)."""
    return {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 3,
                "nodes": [
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 7,
                            "title": "A bug",
                            "state": "CLOSED",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 8,
                            "title": "Another bug",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "REDACTED",
                        "content": None,
                        "fieldValues": {"nodes": []},
                    },
                ],
            },
        }
    }


async def test_github_read_project_item_state_and_redacted(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node_states(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    items = response.json()["items"]
    # A linked issue carries its open/closed state.
    assert items[0]["state"] == "closed"
    assert items[1]["state"] == "open"
    # A redacted item is marked as such and leaks no content.
    assert items[2]["type"] == "REDACTED"
    assert items[2]["repo"] == "" and items[2]["number"] == 0
    assert items[2]["state"] == ""


async def test_github_read_project_item_assignees_and_labels(tmp_path, monkeypatch):
    # Linked issues/PRs carry their real assignees and labels, so a caller can
    # triage a board by person or label (the capability the docs advertise).
    node = {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 2,
                "nodes": [
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 7,
                            "title": "Bug",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "acme/widgets"},
                            "assignees": {"nodes": [{"login": "octocat"}, {"login": "hubot"}]},
                            "labels": {"nodes": [{"name": "bug"}, {"name": "priority:high"}]},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "DRAFT_ISSUE",
                        "content": {"__typename": "DraftIssue", "title": "An idea"},
                        "fieldValues": {"nodes": []},
                    },
                ],
            },
        }
    }
    _install_mock_client(monkeypatch, _graphql_handler(org=node, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    items = response.json()["items"]
    assert items[0]["assignees"] == ["octocat", "hubot"]
    assert items[0]["labels"] == ["bug", "priority:high"]
    # A draft item has no linked issue/PR, so both lists stay empty.
    assert items[1]["assignees"] == [] and items[1]["labels"] == []


def _counts_handler(*, read_node, options, aggregates, status_counts, fail_dsl=False, captured_variables=None):
    """Route POST /graphql three ways by GraphQL operation name: the enumeration
    read (ProjectRead), the aggregate count query (ProjectCounts), and the
    per-status column query (ProjectStatusCounts). fail_dsl simulates the
    board-filter DSL being rejected by the API, so the count path falls back to
    sampling the enumerated items."""

    def handler(request: httpx.Request) -> Response:
        if not request.url.path.endswith("/graphql"):
            return Response(404, json={"message": "Not Found"})
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "ProjectStatusCounts" in query:
            if captured_variables is not None:
                captured_variables.update(payload["variables"])
            node = {"projectV2": {f"s{i}": {"totalCount": c} for i, c in enumerate(status_counts)}}
            return Response(200, json={"data": {"organization": node, "user": None}})
        if "ProjectCounts" in query:
            if fail_dsl:
                return Response(200, json={"errors": [{"message": "unknown query filter"}]})
            node = {
                "projectV2": {
                    "statusField": {"options": [{"name": o} for o in options]},
                    **{alias: {"totalCount": n} for alias, n in aggregates.items()},
                }
            }
            return Response(200, json={"data": {"organization": node, "user": None}})
        return Response(200, json={"data": {"organization": read_node, "user": None}})

    return handler


async def test_github_read_project_authoritative_counts(tmp_path, monkeypatch):
    # Server-side count queries succeed -> exact, authoritative breakdowns whose
    # status columns reconcile to the total.
    handler = _counts_handler(
        read_node=_read_node(),  # totalCount 2 -> matches the authoritative total
        options=["Backlog", "In progress", "Done"],
        # A board draft matches is:issue too, so is:issue counts the draft (1) and
        # board_draft (is:draft is:issue) isolates it; issue = 1 - 1 = 0 real
        # issues, the draft lands in the draft bucket, and the PR under is:pr.
        aggregates={
            "total": 2,
            "issue": 1,
            "pull_request": 1,
            "board_draft": 1,
            "opened": 1,
            "closed": 1,
            "no_status": 0,
        },
        status_counts=[1, 1, 0],
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    body = response.json()
    counts = body["counts"]
    assert counts["authoritative"] is True
    assert counts["total"] == 2
    assert counts["archived_policy"] == "excluded"
    # The authoritative total agrees with the legacy total_count (same board scalar) —
    # only true because both exclude archived items by default. If archived_policy
    # ever changes to include archived items, this assertion will start failing on
    # boards that actually have archived items.
    assert counts["total"] == body["total_count"]
    assert counts["by_status"] == [
        {"name": "Backlog", "count": 1},
        {"name": "In progress", "count": 1},
        {"name": "Done", "count": 0},
    ]
    assert counts["no_status"] == 0
    # The status columns plus the blank column reconcile to the total.
    assert sum(c["count"] for c in counts["by_status"]) + counts["no_status"] == counts["total"]
    # by_type partitions the total; redacted is derived (total - issue - pr - draft).
    assert counts["by_type"] == {"issue": 0, "pull_request": 1, "draft": 1, "redacted": 0}
    assert sum(counts["by_type"].values()) == counts["total"]
    assert counts["by_state"] == {"open": 1, "closed": 1}


async def test_github_authoritative_by_type_partitions_despite_draft_overlap(tmp_path, monkeypatch):
    # A board of {board draft, draft PR, issue, merged PR}: is:issue counts the
    # real issue AND the board draft (2), is:pr counts both PRs (2), is:draft
    # counts the board draft AND the draft PR. Naively summing is:issue + is:pr +
    # is:draft overcounts (6 > total 4). board_draft (is:draft is:issue) isolates
    # the board draft so the buckets partition the total exactly.
    handler = _counts_handler(
        read_node=_read_node(),
        options=["Todo", "Done"],
        aggregates={
            "total": 4,
            "issue": 2,  # real issue + board draft (a board draft matches is:issue)
            "pull_request": 2,  # merged PR + draft PR
            "board_draft": 1,  # is:draft is:issue -> just the board draft
            "opened": 2,
            "closed": 2,
            "no_status": 0,
        },
        status_counts=[3, 1],
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is True
    # issue = is:issue(2) - board_draft(1); pr = is:pr(2); draft = board_draft(1).
    assert counts["by_type"] == {"issue": 1, "pull_request": 2, "draft": 1, "redacted": 0}
    assert sum(counts["by_type"].values()) == counts["total"] == 4


async def test_github_authoritative_counts_downgrade_when_status_does_not_reconcile(tmp_path, monkeypatch):
    # Both count queries "succeed", but the status columns + no_status (2) don't
    # reconcile to the total (91) -- exactly what a non-"Status" single-select or
    # a `"`-in-name option can silently produce. The self-check must refuse the
    # authoritative stamp and degrade to the sampled fallback rather than ship
    # wrong numbers with full authority.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91
    handler = _counts_handler(
        read_node=node,
        options=["Todo", "Done"],
        aggregates={
            "total": 91,
            "issue": 91,
            "pull_request": 0,
            "board_draft": 0,
            "opened": 4,
            "closed": 6,
            "no_status": 0,
        },
        status_counts=[1, 1],  # 1 + 1 + no_status(0) = 2, not 91
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["counted_items"] == 3  # bucketed from the enumerated sample, not the bogus server counts


def test_project_filter_value_escapes_status_option_delimiters():
    # Status names are board-controlled. They are GraphQL variables, but their
    # contents are parsed by GitHub's Projects filter DSL before being counted.
    assert _project_filter_value('Ready "for QA"') == r'Ready \"for QA\"'
    assert _project_filter_value(r"Needs \ review") == r"Needs \\ review"


async def test_github_project_status_count_escapes_filter_values(tmp_path, monkeypatch):
    captured_variables = {}
    handler = _counts_handler(
        read_node=_read_node(),
        options=['Ready "for QA"', r"Needs \ review"],
        aggregates={
            "total": 2,
            "issue": 1,
            "pull_request": 1,
            "board_draft": 0,
            "opened": 2,
            "closed": 0,
            "no_status": 0,
        },
        status_counts=[1, 1],
        captured_variables=captured_variables,
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    assert captured_variables["q0"] == r'status:"Ready \"for QA\""'
    assert captured_variables["q1"] == r'status:"Needs \\ review"'


async def test_github_status_counts_null_project_falls_back_instead_of_zeroing(tmp_path, monkeypatch):
    # A1 (aggregates) succeeds, but A2 (per-status counts) comes back with
    # projectV2: null -- exactly what GraphQL non-null bubbling produces when a
    # single s{i} alias errors. This must drop the whole count attempt to the
    # sampled fallback, not report every status column as 0 while still claiming
    # authoritative: true.
    # totalCount raised well above what's actually enumerated so a fallback to
    # sampling is unambiguously non-authoritative (mirrors the truncated-board
    # fallback test below), isolating this test to the null-projectV2 defect.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91

    def handler(request: httpx.Request) -> Response:
        if not request.url.path.endswith("/graphql"):
            return Response(404, json={"message": "Not Found"})
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "ProjectStatusCounts" in query:
            return Response(
                200,
                json={
                    "data": {"organization": {"projectV2": None}, "user": None},
                    "errors": [{"message": "something went wrong resolving Query.organization.projectV2"}],
                },
            )
        if "ProjectCounts" in query:
            agg_node = {
                "projectV2": {
                    "statusField": {"options": [{"name": "Todo"}, {"name": "Done"}]},
                    "total": {"totalCount": 91},
                    "issue": {"totalCount": 91},
                    "pull_request": {"totalCount": 0},
                    "board_draft": {"totalCount": 0},
                    "opened": {"totalCount": 4},
                    "closed": {"totalCount": 6},
                    "no_status": {"totalCount": 0},
                }
            }
            return Response(200, json={"data": {"organization": agg_node, "user": None}})
        return Response(200, json={"data": {"organization": node, "user": None}})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["by_status"] != [
        {"name": "Todo", "count": 0},
        {"name": "Done", "count": 0},
    ]


async def test_github_read_project_counts_fall_back_to_sample(tmp_path, monkeypatch):
    # The board reports 91 items but only a page enumerated, and the count DSL is
    # rejected -> counts fall back to bucketing the returned items, flagged as a
    # non-authoritative sample so the caller never mistakes it for a full count.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91
    handler = _counts_handler(
        read_node=node, options=[], aggregates={}, status_counts=[], fail_dsl=True
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["total"] == 91
    assert counts["counted_items"] == 3
    # Bucketed from the 3 enumerated items: issue(closed), issue(open), redacted.
    assert counts["by_state"] == {"open": 1, "closed": 1}
    assert counts["by_type"]["issue"] == 2
    assert counts["by_type"]["redacted"] == 1


def test_sampled_counts_counts_merged_pr_as_closed():
    # In the sampled fallback a merged PR (state="merged") must count as closed,
    # matching the authoritative is:closed bucket — not fall through to neither.
    from collab_hub_api.connectors.github_client import _sampled_counts
    from collab_hub_api.connectors.models import GitHubProjectItem

    items = [
        GitHubProjectItem(type="PULL_REQUEST", status="Done", state="merged"),
        GitHubProjectItem(type="PULL_REQUEST", status="Review", state="open"),
        GitHubProjectItem(type="ISSUE", status="Done", state="closed"),
    ]
    counts = _sampled_counts(total=3, items=items)
    assert counts.by_state == {"open": 1, "closed": 2}  # merged PR + closed issue
    assert counts.authoritative is True  # whole board enumerated (counted == total)


async def test_github_projects_do_not_expose_token(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        list_body = (
            await client.post(
                "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "openteams-ai"}
            )
        ).text
        read_body = (
            await client.post(
                "/v1/connectors/github/projects/1/read",
                headers=_auth_header(),
                json={"owner": "openteams-ai"},
            )
        ).text
    assert STATIC_TOKEN not in list_body
    assert STATIC_TOKEN not in read_body


async def test_github_status_handles_form_encoded_broker_token(tmp_path, monkeypatch):
    # GitHub's OAuth token endpoint returns form-urlencoded, and Keycloak brokers
    # it back that way (unlike the JSON Slack/Google IdPs). The token provider
    # must parse it rather than choke on "invalid JSON".
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/broker/github/token"):
            return Response(
                200,
                text="access_token=gho_brokered_token&scope=repo%2Cread%3Aorg&token_type=bearer",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        if path.endswith("/user"):
            return Response(200, json={"login": "octocat"})
        if path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(
        _config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/github/token",
        )
    )
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "octocat"


# --- search ergonomics: clearer 422 on a bad repo/org qualifier ----------


async def test_github_search_bad_repo_qualifier_maps_to_422(tmp_path, monkeypatch):
    # GitHub returns 422 "Validation Failed" when a repo:/org: qualifier points
    # at something that doesn't exist / isn't visible. Surface a caller-fixable
    # 422 with guidance, not a generic 502.
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(422, json={"message": "Validation Failed"})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "is:pr", "repo": "ghost/nope"},
        )
    assert response.status_code == 422
    assert "repo" in response.json()["detail"].lower()


# --- repo discovery: list_github_repos -----------------------------------


def _repo_node(name):
    return {
        "full_name": f"acme/{name}",
        "description": f"{name} service",
        "open_issues_count": 3,
        "updated_at": "2026-08-01T00:00:00Z",
        "private": True,
        "archived": False,
    }


async def test_github_list_repos_org(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/orgs/acme/repos"):
            return Response(200, json=[_repo_node("widgets"), _repo_node("gadgets")])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "acme"}
        )
    assert response.status_code == 200
    repos = response.json()["repos"]
    assert [r["full_name"] for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert repos[0]["open_issues"] == 3


async def test_github_list_repos_falls_back_to_user(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/orgs/octocat/repos"):
            return Response(404, json={"message": "Not Found"})
        if request.url.path.endswith("/users/octocat/repos"):
            return Response(200, json=[_repo_node("dotfiles")])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "octocat"}
        )
    assert response.status_code == 200
    assert len(response.json()["repos"]) == 1


async def test_github_list_repos_rejects_bad_owner(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "bad/owner"}
        )
    assert response.status_code == 422
