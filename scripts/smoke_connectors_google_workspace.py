"""Smoke assertions for the Collab Hub Gmail and Google Calendar v1 contracts."""

from __future__ import annotations

import argparse
import asyncio

import httpx
from smoke_connectors_google_drive import make_bearer_token


async def smoke_google_workspace_connectors(
    base_url: str, *, bearer_token: str | None
) -> None:
    token = bearer_token or make_bearer_token(
        "workspace-smoke-user",
        "workspace-smoke-org",
        "workspace-smoke-workspace",
    )
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=20
    ) as client:
        gmail_status = await client.get("/v1/connectors/gmail/status")
        gmail_status.raise_for_status()
        assert gmail_status.json()["connected"] is True

        gmail_search = await client.post(
            "/v1/connectors/gmail/search",
            json={"query": "connector rollout", "limit": 5},
        )
        gmail_search.raise_for_status()
        assert gmail_search.json()["messages"][0]["id"] == "message-1"
        assert gmail_search.json()["next_page_token"] == "gmail-page-2"
        assert gmail_search.json()["content_trust"] == "external_untrusted"

        gmail_filter_only = await client.post(
            "/v1/connectors/gmail/search",
            json={"query": "", "label_ids": ["INBOX"], "limit": 1},
        )
        gmail_filter_only.raise_for_status()

        gmail_search_page_2 = await client.post(
            "/v1/connectors/gmail/search",
            json={
                "query": "connector rollout",
                "limit": 5,
                "page_token": gmail_search.json()["next_page_token"],
            },
        )
        gmail_search_page_2.raise_for_status()
        assert gmail_search_page_2.json()["messages"][0]["id"] == "message-2"
        assert gmail_search_page_2.json()["next_page_token"] == ""

        gmail_read = await client.post(
            "/v1/connectors/gmail/messages/message-1/read",
            json={"max_chars": 100},
        )
        gmail_read.raise_for_status()
        assert "rollout checklist" in gmail_read.json()["text"]
        assert "[link]" in gmail_read.json()["text"]

        historical_search = await client.post(
            "/v1/connectors/gmail/search",
            json={
                "query": "apollo-history-proof Zephyr",
                "since_date": "2020-01-01",
                "until_date": "2023-12-31",
                "limit": 1,
            },
        )
        historical_search.raise_for_status()
        historical_page_1 = historical_search.json()
        assert historical_page_1["messages"][0]["id"] == "historical-message-2023"
        assert historical_page_1["messages"][0]["sent_at"].startswith("2023-02-14")
        assert historical_page_1["next_page_token"] == "history-page-2"

        historical_search_page_2 = await client.post(
            "/v1/connectors/gmail/search",
            json={
                "query": "apollo-history-proof Zephyr",
                "since_date": "2020-01-01",
                "until_date": "2023-12-31",
                "limit": 1,
                "page_token": historical_page_1["next_page_token"],
            },
        )
        historical_search_page_2.raise_for_status()
        historical_page_2 = historical_search_page_2.json()
        assert historical_page_2["messages"][0]["id"] == "historical-message-2021"
        assert historical_page_2["messages"][0]["sent_at"].startswith("2021-09-30")
        assert historical_page_2["next_page_token"] == ""

        historical_read_2023 = await client.post(
            "/v1/connectors/gmail/messages/historical-message-2023/read",
            json={"max_chars": 1_000},
        )
        historical_read_2023.raise_for_status()
        historical_text_2023 = historical_read_2023.json()["text"]
        assert "renewal was approved for two years" in historical_text_2023
        assert "Mark owns the security addendum" in historical_text_2023
        assert "[link]" in historical_text_2023

        historical_read_2021 = await client.post(
            "/v1/connectors/gmail/messages/historical-message-2021/read",
            json={"max_chars": 1_000},
        )
        historical_read_2021.raise_for_status()
        historical_text_2021 = historical_read_2021.json()["text"]
        assert "2021 Zephyr kickoff" in historical_text_2021
        assert "PRIVATE ATTACHMENT CONTENT" not in historical_text_2021

        calendar_status = await client.get("/v1/connectors/google-calendar/status")
        calendar_status.raise_for_status()
        assert calendar_status.json()["connected"] is True

        calendar_search = await client.post(
            "/v1/connectors/google-calendar/search",
            json={"query": "connector", "calendar_ids": ["primary"], "limit": 1},
        )
        calendar_search.raise_for_status()
        assert calendar_search.json()["events"][0]["id"] == "event-1"
        assert calendar_search.json()["next_cursor"]
        assert calendar_search.json()["content_trust"] == "external_untrusted"

        calendar_search_page_2 = await client.post(
            "/v1/connectors/google-calendar/search",
            json={
                "query": "connector",
                "calendar_ids": ["primary"],
                "limit": 1,
                "cursor": calendar_search.json()["next_cursor"],
            },
        )
        calendar_search_page_2.raise_for_status()
        assert calendar_search_page_2.json()["events"][0]["id"] == "event-2"
        assert calendar_search_page_2.json()["next_cursor"] == ""

        calendar_read = await client.post(
            "/v1/connectors/google-calendar/calendars/primary/events/event-1/read",
            json={"max_chars": 100},
        )
        calendar_read.raise_for_status()
        assert calendar_read.json()["event"]["summary"] == "Connector planning"
        assert "html_url" not in calendar_read.json()["event"]

        combined = "".join(
            response.text
            for response in (
                gmail_status,
                gmail_search,
                gmail_filter_only,
                gmail_search_page_2,
                gmail_read,
                historical_search,
                historical_search_page_2,
                historical_read_2023,
                historical_read_2021,
                calendar_status,
                calendar_search,
                calendar_search_page_2,
                calendar_read,
            )
        )
        assert "fake-google-access-token" not in combined
        assert "https://unsafe.example.test" not in combined


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        smoke_google_workspace_connectors(args.base_url, bearer_token=args.bearer_token)
    )


if __name__ == "__main__":
    main()
