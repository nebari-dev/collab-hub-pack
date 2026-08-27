from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str, org: str = "org-a", workspace: str = "workspace-a") -> dict[str, str]:
    return {"IdToken-test": _jwt({"preferred_username": user, "org_id": org, "workspace_id": workspace})}


def _config(tmp_path, *, history_backend: str = "memory") -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": history_backend},
                "mcp_session_manager_enabled": False,
            },
        }
    )


@pytest_asyncio.fixture
async def history_app(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


@pytest_asyncio.fixture
async def no_db_history_client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    # No `memory` override and no shared frames.postgres URL ⟹ history is
    # unavailable (the only off state — there is no per-feature `disabled`).
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_config(tmp_path, history_backend=""))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def create_frame(client, user: str = "alice", name: str = "Team Frame", body: str = "# Secret Body") -> dict:
    response = await client.post(
        "/v1/frames",
        cookies=auth_cookie(user),
        json={"name": name, "tags": ["team"], "body": body},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def get_history(client, frame_id: str, user: str = "alice", **params) -> dict:
    response = await client.get(
        f"/v1/frames/{frame_id}/history",
        cookies=auth_cookie(user),
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Recording: one row per mutation, correct event/actor/detail -------------


async def test_create_records_single_created_event_with_name(history_app):
    client, _ = history_app
    frame = await create_frame(client, name="Onboarding")

    body = await get_history(client, frame["id"])

    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["event"] == "created"
    assert entry["actor"] == "alice"
    assert entry["detail"] == {"name": "Onboarding"}


async def test_actor_is_hub_user_not_document_author(history_app):
    client, _ = history_app
    # The body embeds an "author" the recorder must never adopt as the actor.
    frame = await create_frame(client, user="alice", body="author: mallory\n# Body")

    entry = (await get_history(client, frame["id"]))["entries"][0]

    assert entry["actor"] == "alice"


async def test_update_records_changed_scalar_fields(history_app):
    client, _ = history_app
    frame = await create_frame(client, name="Before")

    response = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "After", "description": "now described", "tags": ["team"], "body": "# Body"},
    )
    assert response.status_code == 200, response.text

    entries = (await get_history(client, frame["id"]))["entries"]
    update_rows = [e for e in entries if e["event"] == "updated"]
    assert len(update_rows) == 1
    assert update_rows[0]["detail"]["name"] == {"from": "Before", "to": "After"}
    assert update_rows[0]["detail"]["description"] == {"from": "", "to": "now described"}


async def test_visibility_change_records_dedicated_event(history_app):
    client, _ = history_app
    frame = await create_frame(client, name="Vis")

    response = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Vis", "visibility": "internal", "tags": ["team"], "body": "# Body"},
    )
    assert response.status_code == 200, response.text

    events = [e["event"] for e in (await get_history(client, frame["id"]))["entries"]]
    assert "visibility_changed" in events
    vis = next(e for e in (await get_history(client, frame["id"]))["entries"] if e["event"] == "visibility_changed")
    assert vis["detail"] == {"visibility": {"from": "private", "to": "internal"}}


async def test_owners_change_records_added_and_removed(history_app):
    client, _ = history_app
    frame = await create_frame(client)

    response = await client.post(
        f"/v1/frames/{frame['id']}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert response.status_code == 200, response.text

    entry = next(e for e in (await get_history(client, frame["id"]))["entries"] if e["event"] == "owners_changed")
    assert entry["detail"] == {"added": ["bob"], "removed": []}


async def test_readers_change_records_added_and_removed(history_app):
    client, _ = history_app
    frame = await create_frame(client)
    await client.post(f"/v1/frames/{frame['id']}/publish", cookies=auth_cookie("alice"))

    response = await client.put(
        f"/v1/frames/{frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"readers": ["carol"]},
    )
    assert response.status_code == 200, response.text

    entry = next(e for e in (await get_history(client, frame["id"]))["entries"] if e["event"] == "readers_changed")
    assert entry["detail"] == {"added": ["carol"], "removed": []}


async def test_publish_and_unpublish_record_events(history_app):
    client, _ = history_app
    frame = await create_frame(client)

    await client.post(f"/v1/frames/{frame['id']}/publish", cookies=auth_cookie("alice"))
    await client.post(f"/v1/frames/{frame['id']}/unpublish", cookies=auth_cookie("alice"))

    events = [e["event"] for e in (await get_history(client, frame["id"]))["entries"]]
    assert "published" in events
    assert "unpublished" in events
    published = next(e for e in (await get_history(client, frame["id"]))["entries"] if e["event"] == "published")
    assert published["detail"] == {}


async def test_detail_never_contains_body(history_app):
    client, _ = history_app
    secret = "# DistinctiveSecretBodyContent-XYZ"
    frame = await create_frame(client, body=secret)
    await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Renamed", "tags": ["team"], "body": secret + " more"},
    )

    entries = (await get_history(client, frame["id"]))["entries"]
    for entry in entries:
        serialized = json.dumps(entry["detail"])
        assert "body" not in serialized
        assert "DistinctiveSecretBodyContent" not in serialized


# --- Deletion: history survives, deletion recorded ---------------------------


async def test_history_survives_frame_deletion(history_app):
    client, app = history_app
    frame = await create_frame(client)
    frame_id = frame["id"]

    response = await client.delete(f"/v1/frames/{frame_id}", cookies=auth_cookie("alice"))
    assert response.status_code == 204

    # The frame is gone, so the gated endpoint 404s, but the rows persist and
    # the deletion itself was recorded — verify at the store level.
    store = app.state.history_store
    entries = store.query("org-a", "workspace-a", "frame", frame_id, limit=50)
    events = [e.event for e in entries]
    assert "created" in events
    assert "deleted" in events

    endpoint = await client.get(f"/v1/frames/{frame_id}/history", cookies=auth_cookie("alice"))
    assert endpoint.status_code == 404


# --- Access control ----------------------------------------------------------


async def test_history_404_for_user_without_read_access(history_app):
    client, _ = history_app
    frame = await create_frame(client, user="alice")  # private, unpublished

    response = await client.get(
        f"/v1/frames/{frame['id']}/history",
        cookies=auth_cookie("eve"),
    )
    assert response.status_code == 404


# --- Pagination --------------------------------------------------------------


async def test_cursor_pagination_walks_all_rows_newest_first(history_app):
    client, _ = history_app
    frame = await create_frame(client, name="v0")
    # created + 5 updates = 6 rows total.
    for i in range(1, 6):
        response = await client.put(
            f"/v1/frames/{frame['id']}",
            cookies=auth_cookie("alice"),
            json={"name": f"v{i}", "tags": ["team"], "body": "# Body"},
        )
        assert response.status_code == 200

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["before"] = cursor
        body = await get_history(client, frame["id"], **params)
        assert body["entries"], "the walk must never serve an empty page"
        seen.extend(e["id"] for e in body["entries"])
        cursor = body["next"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10  # guard against a runaway walk

    # 6 rows / limit 2 is an exact multiple: exactly 3 full pages, no empty tail.
    assert pages == 3
    # All six rows returned exactly once.
    assert len(seen) == 6
    assert len(set(seen)) == 6

    # Newest-first: created_at is non-increasing across the full walk.
    full = (await get_history(client, frame["id"], limit=200))["entries"]
    timestamps = [e["created_at"] for e in full]
    assert timestamps == sorted(timestamps, reverse=True)
    assert full[0]["event"] == "updated"  # last mutation is newest


async def test_exhausted_page_returns_null_next(history_app):
    client, _ = history_app
    frame = await create_frame(client)

    body = await get_history(client, frame["id"], limit=50)

    assert len(body["entries"]) == 1
    assert body["next"] is None


async def test_malformed_cursor_returns_400(history_app):
    client, _ = history_app
    frame = await create_frame(client)

    response = await client.get(
        f"/v1/frames/{frame['id']}/history",
        cookies=auth_cookie("alice"),
        params={"before": "not-a-valid-cursor!!"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


# --- No DB configured (the only off state) -----------------------------------


async def test_no_db_recording_is_noop_and_endpoint_503(no_db_history_client):
    client = no_db_history_client
    # Mutations still succeed even though history has no backing DB (record is
    # a no-op); the history endpoint reports 503.
    frame = await create_frame(client)

    response = await client.get(
        f"/v1/frames/{frame['id']}/history",
        cookies=auth_cookie("alice"),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "history_unavailable"


async def test_public_frame_history_readable_cross_tenant(history_app):
    # A cross-tenant reader of a published `public` frame can read it, so its
    # history must be queryable too — gated by can_read, queried under the
    # FRAME's stored tenant (not the caller's).
    client, _ = history_app
    frame = await create_frame(client)
    await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": ["team"], "body": "# Secret Body", "visibility": "public"},
    )
    await client.post(f"/v1/frames/{frame['id']}/publish", cookies=auth_cookie("alice"))

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    response = await client.get(f"/v1/frames/{frame['id']}/history", cookies=outsider)
    assert response.status_code == 200
    events = [entry["event"] for entry in response.json()["entries"]]
    # Real events surface (not an empty list from querying the wrong tenant).
    assert "created" in events
    assert "published" in events
