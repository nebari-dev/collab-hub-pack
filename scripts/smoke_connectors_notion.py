"""Smoke assertions for the Collab Hub Notion connector REST contract.

Run against a Collab Hub API configured to talk to scripts/testdata/fake_notion_app.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json

import httpx

# The fake provider's bot token. It must never appear in any connector response.
FAKE_BOT_TOKEN = "secret_fake-notion-bot-token"
PAGE_ID = "0123456789abcdef0123456789abcdef"
DATABASE_ID = "fedcba9876543210fedcba9876543210"


def make_bearer_token(user: str, org: str, workspace: str) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}.{encode({'preferred_username': user, 'org_id': org, 'workspace_id': workspace})}."
    )


async def smoke_notion_connector(base_url: str, *, bearer_token: str | None) -> None:
    token = bearer_token or make_bearer_token("notion-smoke-user", "notion-smoke-org", "notion-smoke-workspace")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20) as unauthenticated_client:
        unauthenticated = await unauthenticated_client.get("/v1/connectors/notion/status")
        assert unauthenticated.status_code == 401

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=20) as client:
        status = await client.get("/v1/connectors/notion/status")
        status.raise_for_status()
        status_payload = status.json()
        assert status_payload["connected"] is True
        assert status_payload["state"] == "connected"
        assert status_payload["scopes"] == ["read_content"]
        assert FAKE_BOT_TOKEN not in status.text

        search = await client.post("/v1/connectors/notion/search", json={"query": "roadmap", "limit": 5})
        search.raise_for_status()
        search_payload = search.json()
        assert search_payload["content_trust"] == "external_untrusted"
        assert next(hit["title"] for hit in search_payload["hits"]) == "Roadmap"
        # Links are dropped (apollo-desktop#365) and the token never leaks.
        assert "notion.so" not in search.text
        assert FAKE_BOT_TOKEN not in search.text

        read = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", json={})
        read.raise_for_status()
        read_payload = read.json()
        assert read_payload["title"] == "Design Notes"
        assert "Second line" in read_payload["text"]
        assert "evil.example" not in read.text
        assert FAKE_BOT_TOKEN not in read.text

        query = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query",
            json={"since_date": "2026-02-01", "time_zone": "UTC"},
        )
        query.raise_for_status()
        query_payload = query.json()
        assert query_payload["rows"][0]["title"] == "Row 1"
        assert FAKE_BOT_TOKEN not in query.text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(smoke_notion_connector(args.base_url, bearer_token=args.bearer_token))


if __name__ == "__main__":
    main()
