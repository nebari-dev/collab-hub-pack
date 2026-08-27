"""Reusable Frames MCP smoke assertions for Collab Hub API deployments."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from collections.abc import Mapping

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


REQUIRED_TOOLS = {"list_frames", "get_frame", "get_active_frames"}
FRAME_RESOURCE_TEMPLATE = "frame://{frame_id}"


def tool_json(result) -> dict:
    """Parse the JSON text payload returned by FastMCP tools over HTTP."""

    return json.loads(result.content[0].text)


def make_id_token(user: str, org: str, workspace: str) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'preferred_username': user, 'org_id': org, 'workspace_id': workspace})}."


def auth_headers(
    *,
    bearer_token: str | None = None,
    id_token: str | None = None,
    user: str = "smoke-user",
    org: str = "smoke-org",
    workspace: str = "smoke-workspace",
) -> dict[str, str]:
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    token = id_token or make_id_token(user, org, workspace)
    return {"Cookie": f"IdToken-smoke={token}"}


async def assert_mcp_contract(
    base_url: str,
    frame_id: str,
    expected_body: str,
    *,
    bearer_token: str | None = None,
    id_token: str | None = None,
    user: str = "smoke-user",
    org: str = "smoke-org",
    workspace: str = "smoke-workspace",
    headers: Mapping[str, str] | None = None,
    require_stored_active: bool = False,
) -> None:
    request_headers = dict(
        headers
        or auth_headers(
            bearer_token=bearer_token,
            id_token=id_token,
            user=user,
            org=org,
            workspace=workspace,
        )
    )

    async with streamablehttp_client(f"{base_url.rstrip('/')}/mcp", headers=request_headers) as (
        read,
        write,
        _session_id,
    ):
        async with ClientSession(read, write) as session:
            initialize_result = await session.initialize()
            assert initialize_result.serverInfo.name == "frames"

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} >= REQUIRED_TOOLS

            listed = await session.call_tool("list_frames", {})
            assert frame_id in listed.content[0].text

            body = await session.call_tool("get_frame", {"id": frame_id})
            assert tool_json(body)["body"] == expected_body

            active = await session.call_tool("get_active_frames", {"ids": [frame_id]})
            assert [item["body"] for item in tool_json(active)["frames"]] == [expected_body]

            if require_stored_active:
                stored_active = await session.call_tool("get_active_frames", {})
                assert expected_body in {
                    item["body"] for item in tool_json(stored_active)["frames"]
                }

            resource_templates = await session.list_resource_templates()
            template_uris = {template.uriTemplate for template in resource_templates.resourceTemplates}
            assert FRAME_RESOURCE_TEMPLATE in template_uris

            resource = await session.read_resource(f"frame://{frame_id}")
            assert expected_body in resource.contents[0].text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL, without /mcp")
    parser.add_argument("--frame-id", required=True)
    parser.add_argument("--expected-body", required=True)
    parser.add_argument("--bearer-token", default=None)
    parser.add_argument("--id-token", default=None)
    parser.add_argument("--user", default="smoke-user")
    parser.add_argument("--org", default="smoke-org")
    parser.add_argument("--workspace", default="smoke-workspace")
    parser.add_argument("--require-stored-active", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        assert_mcp_contract(
            args.base_url,
            args.frame_id,
            args.expected_body,
            bearer_token=args.bearer_token,
            id_token=args.id_token,
            user=args.user,
            org=args.org,
            workspace=args.workspace,
            require_stored_active=args.require_stored_active,
        )
    )


if __name__ == "__main__":
    main()
