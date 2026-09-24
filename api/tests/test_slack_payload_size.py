"""Tests for the Slack payload size limits (issue #139)."""

from __future__ import annotations

import httpx
from httpx import Response

from collab_hub_api.connectors.slack_client import SEARCH_SNIPPET_CHARS, SlackClient


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


LONG_TEXT = "word " * 2_000  # 10,000 characters, far past the snippet limit
SHORT_TEXT = "a short message"


def _search_handler(texts: list[str]):
    def handler(request: httpx.Request) -> Response:
        matches = [
            {"ts": f"1790000000.00000{i}", "user": "U0001", "text": text, "channel": {"id": "C0001", "name": "general"}}
            for i, text in enumerate(texts)
        ]
        return Response(200, json={"ok": True, "messages": {"matches": matches, "paging": {"pages": 1}}})

    return handler


async def test_search_hit_long_text_is_cut_to_a_snippet(monkeypatch):
    _install_mock_client(monkeypatch, _search_handler([LONG_TEXT]))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    [hit] = await slack.search(query="word", limit=5)

    assert hit.truncated is True
    # 500 kept characters plus the ellipsis marking the cut.
    assert len(hit.text) <= SEARCH_SNIPPET_CHARS + 1
    assert hit.text.endswith("…")
    assert LONG_TEXT.startswith(hit.text.removesuffix("…"))


async def test_search_hit_short_text_is_left_alone(monkeypatch):
    _install_mock_client(monkeypatch, _search_handler([SHORT_TEXT]))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    [hit] = await slack.search(query="short", limit=5)

    assert hit.truncated is False
    assert hit.text == SHORT_TEXT


async def test_search_snippet_never_splits_a_slack_link(monkeypatch):
    # A Slack link entity sitting right across the 500-character boundary.
    text = "x" * (SEARCH_SNIPPET_CHARS - 5) + " <https://example.com/very/long/path|the design doc> tail"
    _install_mock_client(monkeypatch, _search_handler([text]))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    [hit] = await slack.search(query="design", limit=5)

    assert "<" not in hit.text
    assert "https://" not in hit.text


async def test_full_text_of_a_cut_search_hit_is_reachable_through_a_read(monkeypatch):
    hit_ts = "1790000000.000000"

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/search.messages"):
            return _search_handler([LONG_TEXT])(request)
        if path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        if path.endswith("/conversations.history"):
            # The read asks for exactly the hit's timestamp, inclusively.
            assert request.url.params["oldest"] == hit_ts
            assert request.url.params["latest"] == hit_ts
            assert request.url.params["inclusive"] == "true"
            return Response(
                200,
                json={"ok": True, "messages": [{"ts": hit_ts, "user": "U0001", "text": LONG_TEXT}], "has_more": False},
            )
        return Response(404, json={"ok": False, "error": "unknown_method"})

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    [hit] = await slack.search(query="word", limit=5)
    assert hit.truncated is True

    messages, _, _ = await slack.read_conversation(channel_id=hit.channel_id, limit=1, oldest=hit.ts, latest=hit.ts)

    assert [message.text for message in messages] == [LONG_TEXT]
