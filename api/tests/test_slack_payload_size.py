"""Tests for the Slack payload size limits (issue #139)."""

from __future__ import annotations

import httpx
import pytest
from httpx import Response
from pydantic import ValidationError

from collab_hub_api.connectors.models import SlackReadRequest, SlackReadResponse, SlackThreadReadRequest
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


async def test_read_messages_do_not_repeat_the_channel_id(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        messages = [{"ts": f"1790000000.00000{i}", "user": "U0001", "text": "hello"} for i in range(3)]
        return Response(200, json={"ok": True, "messages": messages, "has_more": False})

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    messages, has_more, next_cursor = await slack.read_conversation(channel_id="C0001", limit=10)
    response = SlackReadResponse(channel_id="C0001", messages=messages, has_more=has_more, next_cursor=next_cursor)

    # The channel id appears once, at the top of the response, not on every message.
    assert response.model_dump_json().count("C0001") == 1


def _history_handler(history: list[dict], seen_params: list[dict]):
    """A fake channel that honors limit, an inclusive latest, and an inclusive oldest."""

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        params = dict(request.url.params)
        seen_params.append(params)
        limit = int(params.get("limit", "50"))
        if path.endswith("/conversations.history"):
            latest = params.get("latest")
            pool = [m for m in reversed(history) if not latest or float(m["ts"]) <= float(latest)]
        else:
            oldest = params.get("oldest")
            pool = [m for m in history if not oldest or float(m["ts"]) >= float(oldest)]
        return Response(200, json={"ok": True, "messages": pool[:limit], "has_more": len(pool) > limit})

    return handler


def _messages(count: int, size: int) -> list[dict]:
    """``count`` messages of ``size`` characters each, oldest first."""
    return [{"ts": f"17900000{i:02d}.000100", "user": "U0001", "text": "x" * size} for i in range(count)]


async def test_read_stops_at_the_budget_and_points_at_the_first_message_left_out(monkeypatch):
    history = _messages(10, 1_000)
    _install_mock_client(monkeypatch, _history_handler(history, []))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    messages, has_more, next_cursor = await slack.read_conversation(channel_id="C0001", limit=10, max_chars=3_500)

    # Newest first: three 1,000-character messages fit, the fourth would not.
    assert [m.ts for m in messages] == [history[9]["ts"], history[8]["ts"], history[7]["ts"]]
    assert has_more is True
    assert next_cursor == f"ts:{history[6]['ts']}"


async def test_following_the_budget_cursor_reads_every_message_once(monkeypatch):
    history = _messages(10, 1_000)
    seen_params: list[dict] = []
    _install_mock_client(monkeypatch, _history_handler(history, seen_params))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    read: list[str] = []
    cursor = ""
    while True:
        messages, has_more, cursor = await slack.read_conversation(
            channel_id="C0001", limit=10, cursor=cursor, max_chars=3_500
        )
        read.extend(m.ts for m in messages)
        if not has_more:
            break

    assert read == [m["ts"] for m in reversed(history)]  # no gaps, no repeats
    # Our own "ts:" cursor is turned into Slack's latest, never sent to Slack as a cursor.
    assert seen_params[1]["latest"] == history[6]["ts"]
    assert all("cursor" not in params for params in seen_params)


async def test_a_single_message_bigger_than_the_budget_is_still_returned_whole(monkeypatch):
    history = _messages(1, 20_000)
    _install_mock_client(monkeypatch, _history_handler(history, []))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    messages, has_more, _ = await slack.read_conversation(channel_id="C0001", limit=10, max_chars=500)

    assert len(messages) == 1
    assert len(messages[0].text) == 20_000
    assert has_more is False


async def test_thread_read_continues_from_the_budget_cursor(monkeypatch):
    history = _messages(6, 1_000)  # history[0] is the thread's first message
    seen_params: list[dict] = []
    _install_mock_client(monkeypatch, _history_handler(history, seen_params))
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    first, has_more, cursor = await slack.read_thread(
        channel_id="C0001", message_ts=history[0]["ts"], limit=10, max_chars=2_500
    )
    second, _, _ = await slack.read_thread(
        channel_id="C0001", message_ts=history[0]["ts"], limit=10, cursor=cursor, max_chars=2_500
    )

    assert [m.ts for m in first] == [history[0]["ts"], history[1]["ts"]]
    assert has_more is True
    assert seen_params[1]["oldest"] == history[2]["ts"]
    assert [m.ts for m in second] == [history[2]["ts"], history[3]["ts"]]


def test_read_requests_reject_a_budget_outside_the_allowed_range():
    assert SlackReadRequest().max_chars == 12_000
    assert SlackThreadReadRequest().max_chars == 12_000
    with pytest.raises(ValidationError):
        SlackReadRequest(max_chars=0)
    with pytest.raises(ValidationError):
        SlackThreadReadRequest(max_chars=50_001)


async def test_thread_read_does_not_repeat_the_first_message_on_later_pages(monkeypatch):
    # Slack's conversations.replies can return the thread's first message at the top of every page.
    history = _messages(6, 1_000)
    parent = history[0]

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        oldest = request.url.params.get("oldest")
        replies = [m for m in history[1:] if not oldest or float(m["ts"]) >= float(oldest)]
        return Response(200, json={"ok": True, "messages": [parent, *replies], "has_more": False})

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    read: list[str] = []
    cursor = ""
    while True:
        messages, has_more, cursor = await slack.read_thread(
            channel_id="C0001", message_ts=parent["ts"], limit=10, cursor=cursor, max_chars=2_500
        )
        read.extend(m.ts for m in messages)
        if not has_more:
            break

    assert read == [m["ts"] for m in history]  # the first message appears exactly once
