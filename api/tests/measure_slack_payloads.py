"""Measure Slack connector payload sizes on representative fake data (issue #139).

Run from the api/ folder:
    uv run python tests/measure_slack_payloads.py

Not a pytest test (no ``test_`` prefix), so CI does not collect it. The fake
Slack data is seeded, so the numbers are identical on every run and the
before/after comparison is fair.
"""

from __future__ import annotations

import asyncio
import random

import httpx

from collab_hub_api.connectors.models import (
    SlackReadResponse,
    SlackSearchResponse,
    SlackThreadReadResponse,
)
from collab_hub_api.connectors.slack_client import SlackClient

CHANNEL_ID = "C0123456789"
THREAD_TS = "1790000000.000100"
WORDS = (
    "deploy release config helm chart cluster review merge branch token "
    "connector search thread channel incident fix rollback logs error keycloak "
    "the a to and of we should can please check this that it is on for with"
).split()


def _text(rng: random.Random, n_chars: int) -> str:
    out: list[str] = []
    size = 0
    while size < n_chars:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)[:n_chars]


def _length(rng: random.Random, long_share: float) -> int:
    """Mostly short messages, some medium, a few very long (Slack allows ~40k)."""
    roll = rng.random()
    if roll < long_share:
        return rng.randint(5_000, 40_000)
    if roll < long_share + 0.25:
        return rng.randint(300, 2_000)
    return rng.randint(20, 300)


def _message(rng: random.Random, i: int, long_share: float) -> dict:
    return {
        "ts": f"{1790000000 + i * 60}.{i:06d}",
        "user": f"U{rng.randint(1000000, 9999999):07d}",
        "text": _text(rng, _length(rng, long_share)),
        "thread_ts": THREAD_TS if i % 7 == 0 else "",
        "reply_count": rng.randint(0, 12) if i % 7 == 0 else 0,
    }


def _fake_slack(seed: int):
    def handler(request: httpx.Request) -> httpx.Response:
        rng = random.Random(seed)
        path = request.url.path
        if path.endswith("/conversations.info"):
            return httpx.Response(200, json={"ok": True, "channel": {"id": CHANNEL_ID, "is_channel": True}})
        if path.endswith("/search.messages"):
            count = int(request.url.params.get("count", "20"))
            matches = []
            for i in range(count):
                m = _message(rng, i, long_share=0.15)
                m["channel"] = {"id": CHANNEL_ID, "name": "prod-apollo-a4-release"}
                m["username"] = f"user{i}"
                matches.append(m)
            return httpx.Response(200, json={"ok": True, "messages": {"matches": matches, "paging": {"pages": 1}}})
        if path.endswith("/conversations.history") or path.endswith("/conversations.replies"):
            limit = int(request.url.params.get("limit", "50"))
            messages = [_message(rng, i, long_share=0.05) for i in range(limit)]
            return httpx.Response(
                200,
                json={"ok": True, "messages": messages, "has_more": False, "response_metadata": {"next_cursor": ""}},
            )
        return httpx.Response(404, json={"ok": False, "error": "unknown_method"})

    return handler


def _report(label: str, response) -> None:
    size = len(response.model_dump_json())
    print(f"{label:<34} {size:>10,} chars   ~{size // 4:>8,} tokens")


async def main() -> None:
    original = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_fake_slack(seed=139))
        return original(*args, **kwargs)

    httpx.AsyncClient = mock_client
    try:
        slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

        for limit in (20, 100):
            hits, next_page = await slack.search_page(query="release", limit=limit)
            _report(f"search, {limit} hits", SlackSearchResponse(hits=hits, next_page=next_page))

        for limit in (50, 200):
            messages, has_more, cursor = await slack.read_conversation(channel_id=CHANNEL_ID, limit=limit)
            _report(
                f"channel read, {limit} messages",
                SlackReadResponse(channel_id=CHANNEL_ID, messages=messages, has_more=has_more, next_cursor=cursor),
            )

        messages, has_more, cursor = await slack.read_thread(channel_id=CHANNEL_ID, message_ts=THREAD_TS, limit=200)
        _report(
            "thread read, 200 messages",
            SlackThreadReadResponse(
                channel_id=CHANNEL_ID, message_ts=THREAD_TS, messages=messages, has_more=has_more, next_cursor=cursor
            ),
        )
    finally:
        httpx.AsyncClient = original


if __name__ == "__main__":
    asyncio.run(main())
