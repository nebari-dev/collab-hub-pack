from __future__ import annotations

import base64
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response

from collab_hub_api.config import Config
from collab_hub_api.connectors.gmail_client import (
    _decode_body,
    _message_content_info,
    _message_metadata,
    _recipient_headers,
)
from collab_hub_api.connectors.models import GmailMessageMetadata, GmailReadResponse, GmailSearchResponse
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
    # Marks a capped search hit, so it is distinguishable from a short list (#140).
    assert "recipients_omitted" in schemas["GmailMessageMetadata"]["properties"]
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


def test_gmail_decode_body_returns_empty_text_for_malformed_base64():
    # One data character is not valid base64 even once padding is added.
    assert _decode_body("a") == ""
    assert _decode_body("aGVsbG8") == "hello"


## The following tests are parameterized to run against a variety of mailbox configurations.

SNIPPET = "Reminder: the quarterly all-hands is moved to Thursday at 10am."
LABELS = ["INBOX", "UNREAD", "IMPORTANT", "CATEGORY_UPDATES", "Label_42"]

# Every setting, with its default. A case lists only what it changes.
BASE = dict(
    # Fake mailbox (what "Gmail" returns)
    to_count=150,
    cc_count=0,
    labels=LABELS,
    snippet=SNIPPET,
    subject="Q3 all-hands",
    body="The all-hands moves to Thursday. " * 100,
    html_only=False,
    # Request (what the caller asks the Hub for)
    limit=25,
    label_filter=[],  # search request label_ids
    max_chars=12_000,
)

CASES = {
    "worst": {},
    "no-recipients": {"to_count": 0},
    "no-labels": {"labels": []},
    "no-snippet": {"snippet": ""},
    "3-recipients": {"to_count": 3},
    "20-recipients": {"to_count": 20},
    "to-and-cc": {"to_count": 50, "cc_count": 100},
    "many-labels": {"labels": LABELS + ["STARRED", "TRASH", "SPAM"] + [f"Label_{i}" for i in range(10)]},
    "long-body": {"body": "word " * 12_000},
    "long-body-max-cap": {"body": "word " * 12_000, "max_chars": 50_000},
    "html-only": {"html_only": True, "body": "<p>The all-hands moves to Thursday.</p>" * 100},
    "typical": {"to_count": 3, "limit": 10},
}


def _addresses(prefix: str, count: int) -> str:
    return ", ".join(f"{prefix.title()} {i} <{prefix}{i}@example.com>" for i in range(count))


def _fake_gmail(p: dict):
    headers = [
        {"name": "Subject", "value": p["subject"]},
        {"name": "From", "value": "CEO <ceo@example.com>"},
    ]
    if p["to_count"]:
        headers.append({"name": "To", "value": _addresses("person", p["to_count"])})
    if p["cc_count"]:
        headers.append({"name": "Cc", "value": _addresses("cc", p["cc_count"])})

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/users/me/messages"):
            ids = [{"id": f"m{i}"} for i in range(p["limit"])]
            return Response(200, json={"messages": ids, "resultSizeEstimate": len(ids)})
        return Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[-1],
                "threadId": "t1",
                "snippet": p["snippet"],
                "labelIds": p["labels"],
                "internalDate": "1790000000000",
                "payload": {
                    "mimeType": "text/html" if p["html_only"] else "text/plain",
                    "headers": headers,
                    "body": {"data": _encoded(p["body"])},
                },
            },
        )

    return handler


async def _search_and_read(p: dict, tmp_path, monkeypatch) -> tuple[Response, Response]:
    """Run one search and one read of ``m0`` against the fake mailbox described by ``p``."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    _install_mock_client(monkeypatch, _fake_gmail(p))
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            search = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "all-hands", "limit": p["limit"], "label_ids": p["label_filter"]},
            )
            read = await client.post(
                "/v1/connectors/gmail/messages/m0/read",
                headers=_auth_header(),
                json={"max_chars": p["max_chars"]},
            )

            return search, read

@pytest.mark.parametrize("case", CASES)
async def test_gmail_payload_breakdown(case, tmp_path, monkeypatch):
    p = {**BASE, **CASES[case]}  # defaults, overridden by this case
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    hit = search.json()["messages"][0]
    per_hit = {field: len(json.dumps(hit[field])) for field in ("recipients", "label_ids", "snippet")}
    print(f"\n{case:>18}: search={len(search.content):>7}  read={len(read.content):>6}  per-hit={per_hit}")


        # Regression guard for #140: uncapped, this search is ~182 KB (25 hits x 150 recipients).
    if case == "worst":
        assert len(search.content) < 25_000


# Recipients each search hit may carry (#140). Kept as a literal rather than
# imported from gmail_client so this file still imports before the cap exists.
SEARCH_RECIPIENT_CAP = 10


@pytest.mark.parametrize(
    ("to_count", "cc_count"),
    [(0, 0), (3, 0), (10, 0), (11, 0), (150, 0), (5, 20)],
    ids=["none", "under-cap", "at-cap", "one-over-cap", "large-list", "to-then-cc"],
)
async def test_gmail_search_caps_recipients_and_read_keeps_them_all(to_count, cc_count, tmp_path, monkeypatch):
    p = {**BASE, "to_count": to_count, "cc_count": cc_count}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)
    total = to_count + cc_count

    # The read is the full source of truth: every recipient, To before Cc, and
    # nothing omitted, so the marker never appears.
    message = read.json()["message"]
    full = message["recipients"]
    assert [value.split(" ")[0] for value in full] == ["Person"] * to_count + ["Cc"] * cc_count
    assert "recipients_omitted" not in message
    # Over HTTP too, the serializer removes that one key and nothing else.
    fields = set(GmailMessageMetadata.model_fields)
    assert set(message) == fields - {"recipients_omitted"}

    # Each search hit carries only the first addresses of that list. A cut list
    # says how many were dropped; a complete one carries no marker at all.
    omitted = max(0, total - SEARCH_RECIPIENT_CAP)
    for hit in search.json()["messages"]:
        assert len(hit["recipients"]) == min(total, SEARCH_RECIPIENT_CAP)
        assert hit["recipients"] == full[:SEARCH_RECIPIENT_CAP]
        if omitted:
            assert hit["recipients_omitted"] == omitted
            assert set(hit) == fields
        else:
            assert set(hit) == fields - {"recipients_omitted"}


def test_gmail_metadata_serializer_drops_only_a_zero_recipients_omitted():
    # Every field is left at its default -- empty strings, empty lists and a
    # None sent_at -- so a broader drop (exclude_none, exclude_defaults) that
    # removed anything besides recipients_omitted would fail here.
    fields = set(GmailMessageMetadata.model_fields)
    complete = GmailMessageMetadata(id="m0")
    assert set(complete.model_dump(mode="json")) == fields - {"recipients_omitted"}
    assert set(json.loads(complete.model_dump_json())) == fields - {"recipients_omitted"}

    # A cut list keeps every field, the marker included.
    cut = GmailMessageMetadata(id="m0", recipients_omitted=5)
    assert set(cut.model_dump(mode="json")) == fields
    assert cut.model_dump(mode="json")["recipients_omitted"] == 5

    # The response envelopes are untouched: every field, including the
    # untrusted-content boundary (content_trust, security_notice).
    search = GmailSearchResponse(messages=[complete]).model_dump(mode="json")
    assert set(search) == set(GmailSearchResponse.model_fields)
    read = GmailReadResponse(message=complete, text="", truncated=False).model_dump(mode="json")
    assert set(read) == set(GmailReadResponse.model_fields)
    assert read["content_trust"] == "external_untrusted"


# A realistic mix: Gmail's automatic labels plus ones a user or model acts on.
MIXED_LABELS = [
    "INBOX",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "CATEGORY_PERSONAL",
    "CATEGORY_PROMOTIONS",
    "SENT",
    "Label_42",
]
KEPT_LABELS = ["INBOX", "UNREAD", "STARRED", "CATEGORY_PROMOTIONS", "SENT", "Label_42"]


def test_gmail_message_metadata_hides_only_the_given_labels():
    payload = {"id": "m0", "labelIds": [*MIXED_LABELS, 7]}
    # No hidden set (the read's call): every string label, in Gmail's order.
    assert _message_metadata(payload).label_ids == MIXED_LABELS
    # Only the named labels go; everything else keeps its order.
    hidden = _message_metadata(payload, hidden_labels=frozenset({"IMPORTANT", "Label_42"}))
    assert hidden.label_ids == [label for label in MIXED_LABELS if label not in {"IMPORTANT", "Label_42"}]


@pytest.mark.parametrize(
    ("label_filter", "extra_hidden"),
    [([], set()), (["Label_42"], {"Label_42"}), (["INBOX"], {"INBOX"}), ([" Label_42 "], {"Label_42"})],
    ids=["no-filter", "filtered-user-label", "filtered-inbox", "filter-with-whitespace"],
)
async def test_gmail_search_hides_automatic_and_filtered_labels_and_read_keeps_them_all(
    label_filter, extra_hidden, tmp_path, monkeypatch
):
    p = {**BASE, "labels": MIXED_LABELS, "label_filter": label_filter, "to_count": 3}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    # Search drops Gmail's automatic IMPORTANT and CATEGORY_PERSONAL, plus any
    # label the request filtered on, and keeps the rest in Gmail's order.
    expected = [label for label in KEPT_LABELS if label not in extra_hidden]
    for hit in search.json()["messages"]:
        assert hit["label_ids"] == expected

    # The read is untouched: every label, so anything search hides stays
    # reachable (#140 acceptance).
    assert read.json()["message"]["label_ids"] == MIXED_LABELS


async def test_gmail_label_trim_keeps_labels_a_model_acts_on(tmp_path, monkeypatch):
    # Regression guard: widening the hidden set must not silently remove the
    # labels that answer common questions (unread, starred, sent, drafts,
    # spam/trash, non-default tabs, the user's own labels).
    meaningful = ["UNREAD", "STARRED", "SENT", "DRAFT", "SPAM", "TRASH", "CATEGORY_PROMOTIONS", "Label_42"]
    p = {**BASE, "labels": ["INBOX", *meaningful], "to_count": 3}
    search, _read = await _search_and_read(p, tmp_path, monkeypatch)
    for hit in search.json()["messages"]:
        assert hit["label_ids"] == ["INBOX", *meaningful]


# Gmail's snippet is a preview of the body's opening, so realistic fixtures
# derive it from the body rather than inventing unrelated text.
BODY = "The all-hands moves to Thursday at 10am. Bring questions for the Q&A. " * 20
REAL_SNIPPET = BODY[:200].strip()


async def test_gmail_read_drops_the_snippet_that_its_text_repeats(tmp_path, monkeypatch):
    p = {**BASE, "body": BODY, "snippet": REAL_SNIPPET, "to_count": 3}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    # The read's text starts with everything the snippet said, so the snippet
    # is emptied rather than sent twice.
    message = read.json()["message"]
    assert read.json()["text"].startswith(REAL_SNIPPET)
    assert message["snippet"] == ""
    # Regression: the key itself stays, so the read's shape is unchanged.
    assert "snippet" in message

    # Search has no body, so its snippet is the only preview and must remain.
    for hit in search.json()["messages"]:
        assert hit["snippet"] == REAL_SNIPPET


async def test_gmail_read_keeps_the_snippet_when_there_is_no_body_text(tmp_path, monkeypatch):
    # With no extractable body text (e.g. an attachment-only message) the
    # snippet is the only content, so there is nothing to deduplicate against.
    p = {**BASE, "body": "", "snippet": REAL_SNIPPET, "to_count": 3}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["text"] == ""
    assert read.json()["message"]["snippet"] == REAL_SNIPPET


async def test_gmail_read_drops_the_snippet_even_when_text_is_truncated(tmp_path, monkeypatch):
    # A small max_chars is the caller asking for little body text (for example
    # to fetch only the full recipient list), so the snippet is not restored.
    p = {**BASE, "body": BODY, "snippet": REAL_SNIPPET, "to_count": 3, "max_chars": 20}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["truncated"] is True
    assert len(read.json()["text"]) == 20
    assert read.json()["message"]["snippet"] == ""


async def test_gmail_read_still_sanitizes_text_after_the_snippet_is_dropped(tmp_path, monkeypatch):
    # Regression: the snippet was a sanitized field; dropping it must not be
    # the only thing standing between a raw link and the model. The body text
    # carries the same content and must still be link-sanitized.
    body = "Review the plan at https://unsafe.example.test/plan before Friday."
    p = {**BASE, "body": body, "snippet": body, "to_count": 3}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert "unsafe.example.test" not in read.text
    assert read.json()["text"] == "Review the plan at [link] before Friday."
    assert read.json()["message"]["snippet"] == ""
