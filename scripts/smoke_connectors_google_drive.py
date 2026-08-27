"""Smoke assertions for the Collab Hub Google Drive connector REST contract."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json

import httpx


def make_bearer_token(user: str, org: str, workspace: str) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'preferred_username': user, 'org_id': org, 'workspace_id': workspace})}."


async def smoke_google_drive_connector(base_url: str, *, bearer_token: str | None) -> None:
    token = bearer_token or make_bearer_token("drive-smoke-user", "drive-smoke-org", "drive-smoke-workspace")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20) as unauthenticated_client:
        unauthenticated = await unauthenticated_client.get("/v1/connectors/google-drive/status")
        assert unauthenticated.status_code == 401

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=20) as client:
        status = await client.get("/v1/connectors/google-drive/status")
        status.raise_for_status()
        status_payload = status.json()
        assert status_payload["connected"] is True
        assert status_payload["state"] == "connected"
        assert "fake-google-access-token" not in status.text

        search = await client.post(
            "/v1/connectors/google-drive/search",
            json={"query": "healthcare past performance", "limit": 5},
        )
        search.raise_for_status()
        search_payload = search.json()
        assert search_payload["files"][0]["id"] == "file-1"
        assert search_payload["files"][0]["name"] == "Healthcare past performance"
        assert "fake-google-access-token" not in search.text

        read = await client.post(
            "/v1/connectors/google-drive/files/file-1/read",
            json={"max_chars": 48},
        )
        read.raise_for_status()
        read_payload = read.json()
        assert read_payload["file"]["id"] == "file-1"
        assert read_payload["text"].startswith("Implemented healthcare analytics platform.")
        assert len(read_payload["text"]) == 48
        assert read_payload["truncated"] is True
        assert read_payload["unsupported"] is False
        assert "fake-google-access-token" not in read.text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(smoke_google_drive_connector(args.base_url, bearer_token=args.bearer_token))


if __name__ == "__main__":
    main()
