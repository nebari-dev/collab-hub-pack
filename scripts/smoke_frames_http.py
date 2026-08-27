"""End-to-end Frames REST and MCP smoke test for a running Collab Hub API."""

from __future__ import annotations

import argparse
import asyncio

import httpx

from smoke_frames_mcp import auth_headers, assert_mcp_contract


async def run_smoke(args: argparse.Namespace) -> str:
    headers = auth_headers(
        bearer_token=args.bearer_token,
        id_token=args.id_token,
        user=args.user,
        org=args.org,
        workspace=args.workspace,
    )
    base_url = args.base_url.rstrip("/")
    api_base = args.api_base.rstrip("/")
    expected_body = "# Smoke\nUpdated frame body"

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15) as client:
        for attempt in range(30):
            try:
                health = await client.get("/health")
                health.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 29:
                    raise
                await asyncio.sleep(1)

        created = await client.post(
            f"{api_base}/frames",
            json={
                "name": "Smoke Frame",
                "tags": ["smoke", "mcp"],
                "body": "# Smoke\nInitial frame body",
            },
            headers={**headers, "x-request-id": f"{args.request_id_prefix}-create"},
        )
        created.raise_for_status()
        frame_id = created.json()["id"]

        listed = await client.get(f"{api_base}/frames?tag=smoke&owner={args.user}")
        listed.raise_for_status()
        assert frame_id in {item["id"] for item in listed.json()}

        detail = await client.get(f"{api_base}/frames/{frame_id}")
        detail.raise_for_status()
        assert detail.json()["body"] == "# Smoke\nInitial frame body"

        updated = await client.put(
            f"{api_base}/frames/{frame_id}",
            json={"name": "Updated Smoke Frame", "tags": ["smoke"], "body": expected_body},
        )
        updated.raise_for_status()
        assert updated.json()["body"] == expected_body

        suggestion = await client.post(
            f"{api_base}/frames/{frame_id}/suggestions",
            json={"body": "Suggested change"},
        )
        suggestion.raise_for_status()
        suggestion_id = suggestion.json()["id"]

        open_suggestions = await client.get(f"{api_base}/frames/{frame_id}/suggestions?status=open")
        open_suggestions.raise_for_status()
        assert [item["id"] for item in open_suggestions.json()] == [suggestion_id]

        closed = await client.post(f"{api_base}/frames/{frame_id}/suggestions/{suggestion_id}/close")
        closed.raise_for_status()
        assert closed.json()["status"] == "closed"

        if args.check_active_state:
            active_update = await client.put(f"{api_base}/active-frames", json={"frame_ids": [frame_id]})
            active_update.raise_for_status()
            assert active_update.json()["frame_ids"] == [frame_id]

            active_read = await client.get(f"{api_base}/active-frames")
            active_read.raise_for_status()
            assert active_read.json()["frame_ids"] == [frame_id]

        await assert_mcp_contract(
            base_url,
            frame_id,
            expected_body,
            headers=headers,
            require_stored_active=args.check_active_state,
        )

        metrics = await client.get("/metrics")
        metrics.raise_for_status()
        assert "frames_server_http_requests_total" in metrics.text
        assert "frames_server_audit_events_total" in metrics.text

        if not args.keep_frame:
            deleted = await client.delete(f"{api_base}/frames/{frame_id}")
            deleted.raise_for_status()

    return frame_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-base", default="/v1")
    parser.add_argument("--bearer-token", default=None)
    parser.add_argument("--id-token", default=None)
    parser.add_argument("--user", default="smoke-user")
    parser.add_argument("--org", default="smoke-org")
    parser.add_argument("--workspace", default="smoke-workspace")
    parser.add_argument("--request-id-prefix", default="frames-smoke")
    parser.add_argument("--check-active-state", action="store_true")
    parser.add_argument("--keep-frame", action="store_true")
    return parser.parse_args()


def main() -> None:
    frame_id = asyncio.run(run_smoke(parse_args()))
    print(frame_id)


if __name__ == "__main__":
    main()
