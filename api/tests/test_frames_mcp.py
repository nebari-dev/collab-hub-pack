from __future__ import annotations

import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.active_state import InMemoryActiveFrameStore
from collab_hub_api.frames.auth import AuthContext, current_auth_context
from collab_hub_api.frames.mcp_server import create_mcp_server
from collab_hub_api.frames.models import Visibility
from collab_hub_api.frames.store import LocalFsFrameStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from smoke_frames_mcp import assert_mcp_contract  # noqa: E402


def parse_tool_result(result):
    return json.loads(result[0].text)


def free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def auth_cookie(user: str = "smoke-user") -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    token = (
        f"{encode({'alg': 'none'})}."
        f"{encode({'preferred_username': user, 'org_id': 'smoke-org', 'workspace_id': 'smoke-workspace'})}."
    )
    return f"IdToken-smoke={token}"


def start_test_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base_url, timeout=1) as client:
        for _ in range(100):
            try:
                if client.get("/health").status_code == 200:
                    return server, thread, base_url
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("test server did not become healthy")


@pytest.mark.anyio
async def test_mcp_tools_preserve_auth_scope_active_fallback_and_resource(tmp_path):
    store = LocalFsFrameStore(tmp_path)
    active_store = InMemoryActiveFrameStore()
    auth = AuthContext(user="alice", home_org_id="org-a", workspace_id="workspace-a")
    first = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="alice",
        owners=["alice"],
        name="Brand Voice",
        description="",
        visibility=Visibility.private,
        tags=["brand", "sales"],
        body="# Brand\nUse short words.",
    )
    # Owned by bob but published + internal, so alice can read it via can_read.
    second = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="bob",
        owners=["bob"],
        name="Legal Review",
        description="",
        visibility=Visibility.internal,
        tags=["legal"],
        body="# Legal\nDo not promise terms.",
    )
    store.set_published(second.id, True)
    # Owned by alice but unpublished + private: alice still reads it (owner),
    # but it must never surface to non-owners.
    hidden = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="carol",
        owners=["carol"],
        name="Carol Private",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Carol\nInvisible to alice.",
    )
    # Owned by a stranger in another workspace: alice is not an owner and the
    # frame is private, so can_read denies her even cross-tenant.
    other_workspace = store.create_frame(
        org_id=auth.org_id,
        workspace_id="workspace-b",
        created_by="dave",
        owners=["dave"],
        name="Other Workspace",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Other\nInvisible.",
    )
    # Public + published in a DIFFERENT org/workspace: alice can read it by id
    # (cross-tenant public), but it must not surface in her scoped list.
    public_other_tenant = store.create_frame(
        org_id="org-b",
        workspace_id="workspace-z",
        created_by="erin",
        owners=["erin"],
        name="Public Elsewhere",
        description="",
        visibility=Visibility.public,
        tags=["brand"],
        body="# Public\nReadable anywhere.",
    )
    store.set_published(public_other_tenant.id, True)
    active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, [second.id])
    mcp = create_mcp_server(store, active_store=active_store)

    token = current_auth_context.set(auth)
    try:
        all_frames = parse_tool_result(await mcp.call_tool("list_frames", {}))
        assert {item["id"] for item in all_frames["frames"]} == {first.id, second.id}
        assert "body" not in all_frames["frames"][0]
        assert all_frames["frames"][0]["token_estimate"] >= 1

        brand_frames = parse_tool_result(await mcp.call_tool("list_frames", {"tags": ["brand"]}))
        assert [item["id"] for item in brand_frames["frames"]] == [first.id]

        alice_frames = parse_tool_result(await mcp.call_tool("list_frames", {"owner": "alice"}))
        assert [item["id"] for item in alice_frames["frames"]] == [first.id]

        named_frames = parse_tool_result(await mcp.call_tool("list_frames", {"name": "voice"}))
        assert [item["id"] for item in named_frames["frames"]] == [first.id]

        body = parse_tool_result(await mcp.call_tool("get_frame", {"id": first.id}))
        assert body == {"id": first.id, "body": "# Brand\nUse short words."}

        active = parse_tool_result(await mcp.call_tool("get_active_frames", {"ids": [first.id, second.id]}))
        assert [item["body"] for item in active["frames"]] == [
            "# Brand\nUse short words.",
            "# Legal\nDo not promise terms.",
        ]

        stored_active = parse_tool_result(await mcp.call_tool("get_active_frames", {}))
        assert [item["id"] for item in stored_active["frames"]] == [second.id]

        # An empty `ids` list must behave like an omitted arg (return the active
        # set), not return zero Frames. Models commonly call the tool with
        # `ids: []` instead of omitting it.
        empty_ids_active = parse_tool_result(await mcp.call_tool("get_active_frames", {"ids": []}))
        assert [item["id"] for item in empty_ids_active["frames"]] == [second.id]

        templates = await mcp.list_resource_templates()
        assert {template.uriTemplate for template in templates} == {"frame://{frame_id}"}

        resource = await mcp.read_resource(f"frame://{first.id}")
        assert resource[0].content == "# Brand\nUse short words."

        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": other_workspace.id})

        # can_read is enforced: a private, unpublished frame alice does not own
        # is invisible to both get_frame and list_frames.
        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": hidden.id})
        assert hidden.id not in {item["id"] for item in all_frames["frames"]}

        # Cross-tenant `public` read: get_frame reaches a published public frame
        # in another org/workspace by id...
        public_body = parse_tool_result(await mcp.call_tool("get_frame", {"id": public_other_tenant.id}))
        assert public_body == {"id": public_other_tenant.id, "body": "# Public\nReadable anywhere."}
        # ...and the resource too...
        public_resource = await mcp.read_resource(f"frame://{public_other_tenant.id}")
        assert public_resource[0].content == "# Public\nReadable anywhere."
        # ...but list_frames stays tenant-scoped and never discovers it.
        assert public_other_tenant.id not in {item["id"] for item in all_frames["frames"]}
        brand_again = parse_tool_result(await mcp.call_tool("list_frames", {"tags": ["brand"]}))
        assert public_other_tenant.id not in {item["id"] for item in brand_again["frames"]}
    finally:
        current_auth_context.reset(token)

    # The store itself does not apply can_read (that is the MCP/router layer):
    # all three same-workspace frames remain persisted.
    assert len(store.list_frames(auth.org_id, auth.workspace_id)) == 3
    assert store.get_frame(first.id).body == "# Brand\nUse short words."


@pytest.mark.anyio
async def test_mcp_rejects_invalid_frame_ids_before_store_lookup(tmp_path):
    store = LocalFsFrameStore(tmp_path)
    active_store = InMemoryActiveFrameStore()
    auth = AuthContext(user="alice", home_org_id="org-a", workspace_id="workspace-a")
    active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, ["../metadata"])
    mcp = create_mcp_server(store, active_store=active_store)
    store_lookup_called = False

    def fail_get_frame(frame_id):
        nonlocal store_lookup_called
        store_lookup_called = True
        msg = f"store lookup should not receive invalid Frame id {frame_id!r}"
        raise AssertionError(msg)

    store.get_frame = fail_get_frame

    token = current_auth_context.set(auth)
    try:
        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": "../metadata"})
        with pytest.raises(Exception):
            await mcp.call_tool("get_active_frames", {"ids": ["../metadata"]})
        with pytest.raises(Exception):
            await mcp.call_tool("get_active_frames", {})
        with pytest.raises(Exception):
            await mcp.read_resource("frame://../metadata")
    finally:
        current_auth_context.reset(token)

    assert store_lookup_called is False


def test_mcp_http_mount_starts_session_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}},
        }
    )
    app = make_app(config)

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    assert response.status_code == 200
    assert '"name":"frames"' in response.text or '"name": "frames"' in response.text


def test_mcp_http_smoke_helper_parses_tool_json_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}},
        }
    )
    app = make_app(config)
    server, thread, base_url = start_test_server(app, free_tcp_port())
    headers = {"Cookie": auth_cookie()}
    expected_body = "# Smoke\nUpdated frame body"

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=5) as client:
            created = client.post(
                "/v1/frames",
                json={
                    "name": "Smoke Frame",
                    "tags": ["smoke"],
                    "body": "# Smoke\nInitial frame body",
                },
            )
            created.raise_for_status()
            frame_id = created.json()["id"]

            updated = client.put(
                f"/v1/frames/{frame_id}",
                json={"name": "Smoke Frame", "tags": ["smoke"], "body": expected_body},
            )
            updated.raise_for_status()

            active = client.put("/v1/active-frames", json={"frame_ids": [frame_id]})
            active.raise_for_status()

        import asyncio

        asyncio.run(
            assert_mcp_contract(
                base_url,
                frame_id,
                expected_body,
                headers=headers,
                require_stored_active=True,
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
