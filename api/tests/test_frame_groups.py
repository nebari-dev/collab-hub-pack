from __future__ import annotations

import base64
import json

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


def _config(tmp_path, *, groups_backend: str = "memory") -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": groups_backend},
                "mcp_session_manager_enabled": False,
            },
        }
    )


@pytest_asyncio.fixture
async def groups_app(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


@pytest_asyncio.fixture
async def client(groups_app):
    return groups_app[0]


@pytest_asyncio.fixture
async def no_db_groups_client(tmp_path, monkeypatch):
    # No `memory` override and no shared frames.postgres URL ⟹ groups unavailable
    # (the only off state — no per-feature `disabled`).
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_config(tmp_path, groups_backend=""))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def create_frame(
    client,
    user: str = "alice",
    name: str = "Member",
    visibility: str = "private",
    org: str = "org-a",
    workspace: str = "workspace-a",
) -> dict:
    response = await client.post(
        "/v1/frames",
        cookies=auth_cookie(user, org=org, workspace=workspace),
        json={"name": name, "tags": ["team"], "body": "# Body", "visibility": visibility},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def publish(client, frame_id: str, user: str = "alice"):
    response = await client.post(f"/v1/frames/{frame_id}/publish", cookies=auth_cookie(user))
    assert response.status_code == 200, response.text


async def create_group(
    client,
    user: str = "alice",
    frame_ids: list[str] | None = None,
    visibility: str = "private",
    name: str = "Bundle",
) -> dict:
    response = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie(user),
        json={"name": name, "visibility": visibility, "frame_ids": frame_ids or []},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Creation ----------------------------------------------------------------


async def test_create_group_creator_is_sole_owner(client):
    frame = await create_frame(client)
    group = await create_group(client, frame_ids=[frame["id"]])
    assert group["owners"] == ["alice"]
    assert group["created_by"] == "alice"
    assert group["frame_ids"] == [frame["id"]]
    assert group["visibility"] == "private"
    assert group["all_published"] is False


async def test_create_group_empty_frame_ids_is_422(client):
    response = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie("alice"),
        json={"name": "Bundle", "frame_ids": []},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_create_group_dedupes_and_preserves_order(client):
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    group = await create_group(client, frame_ids=[a["id"], b["id"], a["id"]])
    assert group["frame_ids"] == [a["id"], b["id"]]


async def test_create_group_with_unreadable_frame_is_404(client):
    # bob cannot even see alice's private frame, so referencing it reads as
    # not-found (read-then-manage: 404 before 403, no existence leak).
    alice_frame = await create_frame(client, user="alice")
    response = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie("bob"),
        json={"name": "Bundle", "frame_ids": [alice_frame["id"]]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "frame_not_found"


async def test_create_group_with_readable_unowned_frame_is_allowed(client):
    # bob can read alice's published internal frame and does not own it, but
    # bundling only requires readability: 201.
    alice_frame = await create_frame(client, user="alice", visibility="internal")
    await publish(client, alice_frame["id"])
    response = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie("bob"),
        json={"name": "Bundle", "frame_ids": [alice_frame["id"]]},
    )
    assert response.status_code == 201
    assert response.json()["frame_ids"] == [alice_frame["id"]]


async def test_create_group_with_missing_frame_is_404(client):
    response = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie("alice"),
        json={"name": "Bundle", "frame_ids": ["0" * 32]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "frame_not_found"


# --- Visibility gate (PRD §2.3) ---------------------------------------------


async def test_group_with_unpublished_member_is_owner_only(client):
    a = await create_frame(client)
    b = await create_frame(client, name="B")
    await publish(client, a["id"])
    # b is left unpublished -> group is owner-only even though it is internal.
    group = await create_group(client, frame_ids=[a["id"], b["id"]], visibility="internal")

    owner_view = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert owner_view.status_code == 200
    assert owner_view.json()["all_published"] is False

    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 404


async def test_internal_group_readable_once_all_members_published(client):
    # Members must be at least as broad as the group, or effective_visibility caps it.
    a = await create_frame(client, visibility="internal")
    b = await create_frame(client, name="B", visibility="internal")
    group = await create_group(client, frame_ids=[a["id"], b["id"]], visibility="internal")

    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 404

    await publish(client, a["id"])
    await publish(client, b["id"])

    readable = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert readable.status_code == 200
    assert readable.json()["all_published"] is True


async def test_private_group_stays_owner_only_even_when_all_published(client):
    a = await create_frame(client)
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="private")

    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 404
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))).status_code == 200


async def test_cross_workspace_user_cannot_read_internal_published_group(client):
    a = await create_frame(client, visibility="internal")
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="internal")

    other_ws = auth_cookie("carol", workspace="workspace-b")
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=other_ws)).status_code == 404


async def test_public_group_readable_cross_tenant_once_all_published(client):
    a = await create_frame(client, visibility="public")
    group = await create_group(client, frame_ids=[a["id"]], visibility="public")
    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")

    # Any unpublished member keeps even a public group owner-only.
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=outsider)).status_code == 404

    await publish(client, a["id"])

    # All members published: a public group is readable by any authenticated
    # user in ANY tenant (cross-org, cross-workspace).
    readable = await client.get(f"/v1/frame-groups/{group['id']}", cookies=outsider)
    assert readable.status_code == 200
    assert readable.json()["all_published"] is True

    # ...but a cross-tenant public group is still not listed (discovery is scoped).
    assert (await client.get("/v1/frame-groups", cookies=outsider)).json() == []


async def test_effective_visibility_capped_by_narrowest_member(client):
    # A public group is never more visible than its least-visible member: a
    # public group containing a private member is effectively private.
    pub = await create_frame(client, name="Pub", visibility="public")
    priv = await create_frame(client, name="Priv", visibility="private")
    await publish(client, pub["id"])
    await publish(client, priv["id"])
    group = await create_group(client, frame_ids=[pub["id"], priv["id"]], visibility="public")

    # Owner sees it and the derived effective_visibility reads "private".
    owner_view = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert owner_view.status_code == 200
    assert owner_view.json()["all_published"] is True
    assert owner_view.json()["effective_visibility"] == "private"

    # Effectively private ⇒ a same-tenant non-owner and a cross-tenant user are both denied.
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 404
    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=outsider)).status_code == 404

    # Dropping the private member lifts the cap to internal (the remaining member).
    internal_member = await create_frame(client, name="Int", visibility="internal")
    await publish(client, internal_member["id"])
    await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("alice"),
        json={"frame_id": internal_member["id"]},
    )
    await client.delete(f"/v1/frame-groups/{group['id']}/frames/{priv['id']}", cookies=auth_cookie("alice"))
    refreshed = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert refreshed.json()["effective_visibility"] == "internal"
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 200


async def test_list_groups_filters(client):
    a = await create_frame(client, visibility="internal")
    await publish(client, a["id"])
    internal = await create_group(client, frame_ids=[a["id"]], visibility="internal", name="Visible")
    await create_group(client, frame_ids=[a["id"]], visibility="private", name="Hidden")

    # Owner sees both; non-owner sees only the published internal one.
    owner_listing = await client.get("/v1/frame-groups", cookies=auth_cookie("alice"))
    assert {g["name"] for g in owner_listing.json()} == {"Visible", "Hidden"}

    bob_listing = await client.get("/v1/frame-groups", cookies=auth_cookie("bob"))
    assert [g["id"] for g in bob_listing.json()] == [internal["id"]]

    by_published = await client.get("/v1/frame-groups?published=true", cookies=auth_cookie("alice"))
    assert {g["name"] for g in by_published.json()} == {"Visible", "Hidden"}

    by_visibility = await client.get("/v1/frame-groups?visibility=internal", cookies=auth_cookie("alice"))
    assert {g["name"] for g in by_visibility.json()} == {"Visible"}

    by_name = await client.get("/v1/frame-groups?name=vis", cookies=auth_cookie("alice"))
    assert {g["name"] for g in by_name.json()} == {"Visible"}


# --- Membership --------------------------------------------------------------


async def test_add_and_remove_member(client):
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    group = await create_group(client, frame_ids=[a["id"]])

    added = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("alice"),
        json={"frame_id": b["id"]},
    )
    assert added.status_code == 200
    assert added.json()["frame_ids"] == [a["id"], b["id"]]

    removed = await client.delete(
        f"/v1/frame-groups/{group['id']}/frames/{a['id']}",
        cookies=auth_cookie("alice"),
    )
    assert removed.status_code == 200
    assert removed.json()["frame_ids"] == [b["id"]]


async def test_remove_last_member_is_conflict(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])

    response = await client.delete(
        f"/v1/frame-groups/{group['id']}/frames/{a['id']}",
        cookies=auth_cookie("alice"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_frame"


async def test_remove_non_member_is_404(client):
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    group = await create_group(client, frame_ids=[a["id"]])

    response = await client.delete(
        f"/v1/frame-groups/{group['id']}/frames/{b['id']}",
        cookies=auth_cookie("alice"),
    )
    assert response.status_code == 404


async def test_add_unreadable_frame_is_404(client):
    a = await create_frame(client, user="alice")  # private, invisible to bob
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"]])

    # bob cannot see alice's private frame, so adding it reads as not-found.
    response = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("bob"),
        json={"frame_id": a["id"]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "frame_not_found"


async def test_add_readable_unowned_frame_is_allowed(client):
    a = await create_frame(client, user="alice", visibility="internal")
    await publish(client, a["id"])  # bob can now read it, but does not own it
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"]])

    response = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("bob"),
        json={"frame_id": a["id"]},
    )
    assert response.status_code == 200
    assert a["id"] in response.json()["frame_ids"]


async def test_add_frame_as_reader_on_private_frame_is_allowed(client):
    # alice grants bob reader access on a published private frame; bob does not
    # own it, but the reader list is exactly the ACL-lite expansion that
    # make-active (and now group-add) treats as readable.
    a = await create_frame(client, user="alice")
    await publish(client, a["id"])
    added_reader = await client.post(
        f"/v1/frames/{a['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert added_reader.status_code == 200, added_reader.text

    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"]])

    response = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("bob"),
        json={"frame_id": a["id"]},
    )
    assert response.status_code == 200
    assert a["id"] in response.json()["frame_ids"]


async def test_add_public_frame_across_tenants_is_allowed(client):
    # `public` grants cross-tenant READ (mirrors test_public_frame_readable_
    # cross_tenant in test_frames.py); group-add rides the same read check, so
    # a user in a totally different org/workspace may bundle a published
    # public frame into their own group.
    alice_frame = await create_frame(client, user="alice", visibility="public")
    await publish(client, alice_frame["id"])

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    zoe_frame = (
        await client.post(
            "/v1/frames",
            cookies=outsider,
            json={"name": "Zoe's Frame", "tags": [], "body": "# Body", "visibility": "private"},
        )
    ).json()
    group = (
        await client.post(
            "/v1/frame-groups",
            cookies=outsider,
            json={"name": "Zoe's Bundle", "visibility": "private", "frame_ids": [zoe_frame["id"]]},
        )
    ).json()

    response = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=outsider,
        json={"frame_id": alice_frame["id"]},
    )
    assert response.status_code == 200
    assert alice_frame["id"] in response.json()["frame_ids"]


async def test_add_unpublished_frame_by_non_owner_still_404(client):
    # a is unpublished, internal-visibility; even though bob shares alice's
    # tenant (which would grant internal read once published), can_read gates
    # unpublished frames to owners only. The relaxation must not let drafts
    # leak into a group via a same-tenant non-owner.
    a = await create_frame(client, user="alice", visibility="internal")
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"]])

    response = await client.post(
        f"/v1/frame-groups/{group['id']}/frames",
        cookies=auth_cookie("bob"),
        json={"frame_id": a["id"]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "frame_not_found"


# --- Owners ------------------------------------------------------------------


async def test_owner_add_remove_and_last_owner_conflict(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])

    added = await client.post(
        f"/v1/frame-groups/{group['id']}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert added.status_code == 200
    assert added.json()["owners"] == ["alice", "bob"]

    removed = await client.delete(
        f"/v1/frame-groups/{group['id']}/owners/alice",
        cookies=auth_cookie("bob"),
    )
    assert removed.status_code == 200
    assert removed.json()["owners"] == ["bob"]

    last = await client.delete(
        f"/v1/frame-groups/{group['id']}/owners/bob",
        cookies=auth_cookie("bob"),
    )
    assert last.status_code == 409
    assert last.json()["error"]["code"] == "last_owner"


async def test_non_owner_cannot_manage_group(client):
    a = await create_frame(client, visibility="internal")
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="internal")

    # carol can read it (published internal) but cannot manage it.
    response = await client.put(
        f"/v1/frame-groups/{group['id']}",
        cookies=auth_cookie("carol"),
        json={"name": "Hijacked", "description": "", "visibility": "internal"},
    )
    assert response.status_code == 403


async def test_mutation_on_unreadable_group_is_404_not_403(client):
    # A private group (owner-only) is invisible to a non-owner, so a mutation
    # endpoint returns 404 rather than 403 — read-then-manage ordering (Spec 3 §4).
    a = await create_frame(client)
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="private")

    put = await client.put(
        f"/v1/frame-groups/{group['id']}",
        cookies=auth_cookie("carol"),
        json={"name": "Hijacked", "description": "", "visibility": "private"},
    )
    assert put.status_code == 404
    assert put.json()["error"]["code"] == "group_not_found"
    assert (await client.delete(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("carol"))).status_code == 404


async def test_remove_unknown_owner_is_404(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])
    response = await client.delete(
        f"/v1/frame-groups/{group['id']}/owners/nobody",
        cookies=auth_cookie("alice"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "group_not_found"


# --- Update / delete ---------------------------------------------------------


async def test_update_group_fields(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])
    response = await client.put(
        f"/v1/frame-groups/{group['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Renamed", "description": "notes", "visibility": "internal"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "notes"
    assert body["visibility"] == "internal"


async def test_delete_group_leaves_member_frames_intact(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])

    deleted = await client.delete(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))).status_code == 404
    # The member frame still exists.
    assert (await client.get(f"/v1/frames/{a['id']}", cookies=auth_cookie("alice"))).status_code == 200


async def test_deleting_member_prunes_it_from_group(client):
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    group = await create_group(client, frame_ids=[a["id"], b["id"]])

    deleted = await client.delete(f"/v1/frames/{a['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    # The deleted frame is pruned; the group survives with its remaining member.
    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [b["id"]]


async def test_deleting_sole_member_cascade_deletes_group(groups_app):
    client, app = groups_app
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])

    deleted = await client.delete(f"/v1/frames/{a['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    # The group is removed entirely (>=1-member invariant), not left with a stale id.
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))).status_code == 404
    # The cascade is recorded in group history.
    rows = app.state.history_store.query("org-a", "workspace-a", "group", group["id"], 50)
    assert any(r.event == "deleted" and (r.detail or {}).get("reason") == "last_member_deleted" for r in rows)


async def test_deleting_cross_tenant_member_prunes_stale_group_membership(client):
    # Willie's exact repro (PR #46 review): Zoe (tenant Z) bundles Alice's
    # (tenant A) published public Frame into her own group. Alice deletes the
    # Frame from tenant A. Reconciliation must find Zoe's group even though it
    # lives outside the Frame's own org/workspace — the old tenant-scoped
    # lookup missed this and left the group stale.
    alice_frame = await create_frame(client, user="alice", visibility="public")
    await publish(client, alice_frame["id"])

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    group = (
        await client.post(
            "/v1/frame-groups",
            cookies=outsider,
            json={"name": "Zoe's Bundle", "visibility": "private", "frame_ids": [alice_frame["id"]]},
        )
    ).json()

    deleted = await client.delete(f"/v1/frames/{alice_frame['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    # The sole member was deleted -> the group cascades to 404, matching the
    # documented last-member-deleted contract (same as the same-tenant case).
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=outsider)).status_code == 404


async def test_deleting_cross_tenant_non_sole_member_prunes_without_cascade(groups_app):
    # Cross-tenant find_groups_containing -> remove_frame (not cascade): Zoe's
    # (tenant Z) group holds Alice's (tenant A) public frame AND Zoe's own frame.
    # Alice deletes hers; the group must SURVIVE with just Zoe's frame and record
    # a frame_removed event carrying the deletion reason.
    client, app = groups_app
    alice_frame = await create_frame(client, user="alice", visibility="public")
    await publish(client, alice_frame["id"])

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    zoe_frame = (
        await client.post(
            "/v1/frames",
            cookies=outsider,
            json={"name": "Zoe's Frame", "tags": [], "body": "# Body", "visibility": "private"},
        )
    ).json()
    group = (
        await client.post(
            "/v1/frame-groups",
            cookies=outsider,
            json={"name": "Zoe's Bundle", "visibility": "private", "frame_ids": [zoe_frame["id"], alice_frame["id"]]},
        )
    ).json()

    deleted = await client.delete(f"/v1/frames/{alice_frame['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=outsider)
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [zoe_frame["id"]]

    # History lives under the GROUP's own tenant (Z), and carries the reason.
    rows = app.state.history_store.query("org-z", "workspace-z", "group", group["id"], 50)
    assert any(
        r.event == "frame_removed"
        and (r.detail or {}).get("reason") == "member_frame_deleted"
        and (r.detail or {}).get("frame_id") == alice_frame["id"]
        for r in rows
    )


async def test_deleting_middle_member_keeps_group_ordered(groups_app):
    # Non-exact-equality branch of _remove_deleted_member_from_group: delete the
    # MIDDLE member of [A, B, C]; the group survives as [A, C] with the deletion
    # reason recorded (order preserved, not just "a member removed").
    client, app = groups_app
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    c = await create_frame(client, name="C")
    group = await create_group(client, frame_ids=[a["id"], b["id"], c["id"]])

    deleted = await client.delete(f"/v1/frames/{b['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("alice"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [a["id"], c["id"]]

    rows = app.state.history_store.query("org-a", "workspace-a", "group", group["id"], 50)
    assert any(
        r.event == "frame_removed"
        and (r.detail or {}).get("reason") == "member_frame_deleted"
        and (r.detail or {}).get("frame_id") == b["id"]
        for r in rows
    )


async def test_deleting_non_sole_member_records_reason_in_history(groups_app):
    # The frame_removed branch of deletion reconciliation carries a reason so an
    # audit reader can tell the removal was deletion-driven (Trent P3a).
    client, app = groups_app
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    group = await create_group(client, frame_ids=[a["id"], b["id"]])

    deleted = await client.delete(f"/v1/frames/{a['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    rows = app.state.history_store.query("org-a", "workspace-a", "group", group["id"], 50)
    assert any(r.event == "frame_removed" and (r.detail or {}).get("reason") == "member_frame_deleted" for r in rows)


# --- Non-destructive access narrowing ----------------------------------------
# Narrowing a member frame's readability (unpublish, visibility change, reader
# removal, owner changes on either the frame or the group) never mutates group
# membership; the member stays and recovers automatically if access returns.
# Only actual frame DELETION removes membership (see the deletion tests above).


async def test_unpublish_keeps_unreadable_member_in_group(groups_app):
    # bob bundles alice's published internal frame (readable, same tenant) into
    # a group alongside his own frame. Alice unpublishes it -> bob can no longer
    # read it, but membership is non-destructive: it stays, and no removal event
    # is recorded.
    client, app = groups_app
    alice_frame = await create_frame(client, user="alice", visibility="internal")
    await publish(client, alice_frame["id"])
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"], alice_frame["id"]])

    unpublished = await client.post(f"/v1/frames/{alice_frame['id']}/unpublish", cookies=auth_cookie("alice"))
    assert unpublished.status_code == 200

    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]

    rows = app.state.history_store.query("org-a", "workspace-a", "group", group["id"], 50)
    assert not any(r.event in {"frame_removed", "deleted"} for r in rows)


async def test_republish_recovers_member_readability_without_touching_membership(client):
    # After a narrow (unpublish) the member persists but is unreadable to the
    # group owner; republishing restores read access automatically, with no
    # membership change at any point.
    alice_frame = await create_frame(client, user="alice", visibility="internal")
    await publish(client, alice_frame["id"])
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"], alice_frame["id"]])

    await client.post(f"/v1/frames/{alice_frame['id']}/unpublish", cookies=auth_cookie("alice"))
    # Unreadable to bob while unpublished, but still a member.
    assert (await client.get(f"/v1/frames/{alice_frame['id']}", cookies=auth_cookie("bob"))).status_code == 404
    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]

    await publish(client, alice_frame["id"])
    # Readable again with no intervention; membership unchanged throughout.
    assert (await client.get(f"/v1/frames/{alice_frame['id']}", cookies=auth_cookie("bob"))).status_code == 200
    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]


async def test_visibility_narrow_keeps_member_in_group(client):
    # alice narrows an internal member to private; bob (neither owner nor reader)
    # loses read access, but membership persists.
    alice_frame = await create_frame(client, user="alice", visibility="internal")
    await publish(client, alice_frame["id"])
    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"], alice_frame["id"]])

    narrowed = await client.put(
        f"/v1/frames/{alice_frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": [], "body": "# Body", "visibility": "private"},
    )
    assert narrowed.status_code == 200

    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]


async def test_reader_removal_keeps_member_in_group(client):
    # alice grants bob reader access on a published private frame; bob bundles it
    # into his group. Alice revokes the grant -> bob loses read access, but the
    # member stays in the group.
    alice_frame = await create_frame(client, user="alice")
    await publish(client, alice_frame["id"])
    added_reader = await client.post(
        f"/v1/frames/{alice_frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert added_reader.status_code == 200, added_reader.text

    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"], alice_frame["id"]])

    removed_reader = await client.delete(
        f"/v1/frames/{alice_frame['id']}/readers/bob",
        cookies=auth_cookie("alice"),
    )
    assert removed_reader.status_code == 200, removed_reader.text

    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]


async def test_group_owner_change_leaving_no_reader_keeps_membership(client):
    # Trent's P2 scenario: a group ends up with no owner who can read a member.
    # alice's published private frame is readable by bob (reader) but not carol.
    # bob's group (co-owned by carol) drops bob as an owner, leaving only carol
    # — who cannot read the member. Membership must persist regardless.
    alice_frame = await create_frame(client, user="alice")
    await publish(client, alice_frame["id"])
    added_reader = await client.post(
        f"/v1/frames/{alice_frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert added_reader.status_code == 200, added_reader.text

    bob_frame = await create_frame(client, user="bob", name="Bob")
    group = await create_group(client, user="bob", frame_ids=[bob_frame["id"], alice_frame["id"]])
    added_owner = await client.post(
        f"/v1/frame-groups/{group['id']}/owners",
        cookies=auth_cookie("bob"),
        json={"email": "carol"},
    )
    assert added_owner.status_code == 200, added_owner.text

    # Drop bob (the only owner who can read alice's frame), leaving carol alone.
    removed_owner = await client.delete(
        f"/v1/frame-groups/{group['id']}/owners/bob",
        cookies=auth_cookie("bob"),
    )
    assert removed_owner.status_code == 200, removed_owner.text
    assert removed_owner.json()["owners"] == ["carol"]

    # carol still owns and reads the group; the unreadable member is retained.
    detail = await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("carol"))
    assert detail.status_code == 200
    assert detail.json()["frame_ids"] == [bob_frame["id"], alice_frame["id"]]


async def test_frames_group_id_filter(client):
    a = await create_frame(client, name="A")
    b = await create_frame(client, name="B")
    c = await create_frame(client, name="C")
    group = await create_group(client, frame_ids=[a["id"], b["id"]])

    listing = await client.get(f"/v1/frames?group_id={group['id']}", cookies=auth_cookie("alice"))
    assert listing.status_code == 200
    assert {f["id"] for f in listing.json()} == {a["id"], b["id"]}
    assert c["id"] not in {f["id"] for f in listing.json()}


async def test_frames_group_id_filter_requires_readable_group(client):
    # A private group whose member is a published internal frame: the frame is
    # readable by bob, but the group is not — so the group_id filter must not
    # leak membership (Spec 3 §3).
    a = await create_frame(client, user="alice", visibility="internal")
    await publish(client, a["id"])
    group = await create_group(client, user="alice", frame_ids=[a["id"]], visibility="private")

    # Sanity: bob can read the member frame directly, but not the group.
    assert (await client.get(f"/v1/frames/{a['id']}", cookies=auth_cookie("bob"))).status_code == 200
    assert (await client.get(f"/v1/frame-groups/{group['id']}", cookies=auth_cookie("bob"))).status_code == 404

    filtered = await client.get(f"/v1/frames?group_id={group['id']}", cookies=auth_cookie("bob"))
    assert filtered.status_code == 200
    assert filtered.json() == []


# --- History (Spec 2 wired) --------------------------------------------------


async def test_group_mutations_recorded_as_group_history(groups_app):
    client, app = groups_app
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]])
    await client.put(
        f"/v1/frame-groups/{group['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Renamed", "description": "", "visibility": "private"},
    )

    history = await client.get(f"/v1/frame-groups/{group['id']}/history", cookies=auth_cookie("alice"))
    assert history.status_code == 200
    events = [e["event"] for e in history.json()["entries"]]
    assert "created" in events
    assert "updated" in events

    # Rows are tagged entity_type='group' in the shared store, distinct from frames.
    store = app.state.history_store
    group_rows = store.query("org-a", "workspace-a", "group", group["id"], 50)
    assert {r.event for r in group_rows} >= {"created", "updated"}
    assert store.query("org-a", "workspace-a", "frame", group["id"], 50) == []


async def test_group_history_gated_by_can_read(client):
    a = await create_frame(client)
    group = await create_group(client, frame_ids=[a["id"]], visibility="internal")
    # Unpublished member -> owner-only -> bob gets 404 on history too.
    response = await client.get(f"/v1/frame-groups/{group['id']}/history", cookies=auth_cookie("bob"))
    assert response.status_code == 404


async def test_unknown_group_is_404(client):
    response = await client.get(f"/v1/frame-groups/{'0' * 32}", cookies=auth_cookie("alice"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "group_not_found"


async def test_owner_cannot_manage_group_cross_tenant(client):
    # A published public group reads cross-tenant but is not manageable from a
    # foreign tenant context.
    a = await create_frame(client)
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="public")
    gid = group["id"]

    cross = auth_cookie("alice", org="org-z", workspace="workspace-z")
    assert (await client.get(f"/v1/frame-groups/{gid}", cookies=cross)).status_code == 200

    put = await client.put(
        f"/v1/frame-groups/{gid}",
        cookies=cross,
        json={"name": "Hijacked", "description": "", "visibility": "public"},
    )
    assert put.status_code == 403
    assert (await client.delete(f"/v1/frame-groups/{gid}", cookies=cross)).status_code == 403
    assert (
        await client.post(f"/v1/frame-groups/{gid}/owners", cookies=cross, json={"email": "mallory"})
    ).status_code == 403


async def test_public_group_history_readable_cross_tenant(client):
    a = await create_frame(client, visibility="public")
    await publish(client, a["id"])
    group = await create_group(client, frame_ids=[a["id"]], visibility="public")

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    response = await client.get(f"/v1/frame-groups/{group['id']}/history", cookies=outsider)
    assert response.status_code == 200
    events = [entry["event"] for entry in response.json()["entries"]]
    assert "created" in events


# --- No DB configured (the only off state) -----------------------------------


async def test_no_db_group_endpoints_return_503(no_db_groups_client):
    client = no_db_groups_client
    # Every group endpoint 503s when no shared frames Postgres is configured —
    # there is no per-feature toggle and no silent empty/404 fallback.
    listed = await client.get("/v1/frame-groups", cookies=auth_cookie("alice"))
    assert listed.status_code == 503
    assert listed.json()["error"]["code"] == "groups_unavailable"

    # A real, caller-owned member frame so create passes the member check and
    # reaches the (unavailable) group store.
    member = await create_frame(client)
    created = await client.post(
        "/v1/frame-groups",
        cookies=auth_cookie("alice"),
        json={"name": "Bundle", "frame_ids": [member["id"]]},
    )
    assert created.status_code == 503

    fetched = await client.get(f"/v1/frame-groups/{'a' * 32}", cookies=auth_cookie("alice"))
    assert fetched.status_code == 503


async def test_no_db_frame_delete_still_succeeds(no_db_groups_client):
    # Group reconciliation on frame delete must not 503 the delete when there is
    # no groups DB (no groups can exist without one).
    client = no_db_groups_client
    created = await client.post(
        "/v1/frames",
        cookies=auth_cookie("alice"),
        json={"name": "Solo", "tags": ["team"], "body": "# Body"},
    )
    assert created.status_code == 201
    deleted = await client.delete(f"/v1/frames/{created.json()['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204
