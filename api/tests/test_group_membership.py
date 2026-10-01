"""Reading and changing Keycloak group membership from the admin panel.

This is the one place the panel writes to Keycloak. Everything else treats it
as a plain OIDC provider.

The security property under test is not "does it work" but "how little can it
do": adding somebody to a group and being able to reset their password are two
scopes that compose into account takeover, so the credential that does the
first must never hold the second. That rule is asserted by the shape of this
class -- there is no method here that could create, delete or modify an
account -- and by the request paths it is allowed to issue.
"""

from __future__ import annotations

import httpx
import pytest

from collab_hub_api.frames.group_membership import (
    GroupMembershipClient,
    GroupMembershipError,
)

TOKEN = {"access_token": "stub-token", "expires_in": 300}


def membership(handler) -> GroupMembershipClient:
    return GroupMembershipClient(
        token_url="https://kc.example.com/token",
        admin_api_base_url="https://kc.example.com/admin/realms/nebari",
        client_id="collab-admin",
        client_secret="secret",
        group_ids={"/llm": "g-1"},
        transport=httpx.MockTransport(handler),
    )


def token_or(handler):
    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN)
        return handler(request)

    return dispatch


def test_members_of_a_managed_group_come_back_with_their_addresses():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/groups/g-1/members")
        return httpx.Response(
            200,
            json=[
                {"id": "u-1", "username": "alice", "email": "alice@example.com"},
                {"id": "u-2", "username": "bob", "email": "bob@example.com"},
            ],
        )

    members = membership(token_or(handler)).list_members("/llm")

    assert [m.id for m in members] == ["u-1", "u-2"]
    assert members[0].email == "alice@example.com"


def test_granting_and_revoking_use_the_membership_endpoint_only():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    client = membership(token_or(handler))
    client.add_member(user_id="u-1", group_path="/llm")
    client.remove_member(user_id="u-1", group_path="/llm")

    assert seen == [
        ("PUT", "/admin/realms/nebari/users/u-1/groups/g-1"),
        ("DELETE", "/admin/realms/nebari/users/u-1/groups/g-1"),
    ]


def test_a_group_this_deployment_does_not_manage_is_refused_before_any_call():
    """The managed set is a boundary, not a convenience."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made for an unmanaged group")

    with pytest.raises(GroupMembershipError, match="/admins"):
        membership(token_or(handler)).add_member(user_id="u-1", group_path="/admins")


def test_the_client_exposes_no_way_to_create_or_delete_an_account():
    """Membership control plus account control is takeover; keep them apart."""

    surface = {name for name in dir(GroupMembershipClient) if not name.startswith("_")}

    assert surface == {"add_member", "close", "configured", "list_members", "remove_member"}


def test_a_refusal_from_keycloak_is_reported_rather_than_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(GroupMembershipError):
        membership(token_or(handler)).add_member(user_id="u-1", group_path="/llm")


@pytest.mark.parametrize("user_id", ["u-9#", "u-9?x=", "../../groups/g-2", "a/b", "u-9%23"])
def test_a_user_id_cannot_steer_the_request_off_the_membership_endpoint(user_id):
    """The id is caller input. Whatever it contains, it names one path segment:
    a ``#`` that dropped the rest of the path would turn the membership DELETE
    into Keycloak's delete-user call."""

    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path))
        return httpx.Response(204)

    client = membership(token_or(handler))
    client.add_member(user_id=user_id, group_path="/llm")
    client.remove_member(user_id=user_id, group_path="/llm")

    for method, raw_path in seen:
        prefix, _, rest = raw_path.partition(b"/users/")
        assert prefix == b"/admin/realms/nebari", (method, raw_path)
        segment, suffix = rest.split(b"/", 1)
        assert suffix == b"groups/g-1", (method, raw_path)
        assert b"?" not in raw_path and b"#" not in raw_path, (method, raw_path)


@pytest.mark.parametrize("user_id", ["", ".", ".."])
def test_a_user_id_that_is_not_a_path_segment_is_refused_before_any_call(user_id):
    """Encoding leaves dots alone, and a bare ``..`` would climb out of
    ``/users/``: those are refused rather than sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request may be made for {user_id!r}")

    client = membership(token_or(handler))
    with pytest.raises(GroupMembershipError):
        client.add_member(user_id=user_id, group_path="/llm")
    with pytest.raises(GroupMembershipError):
        client.remove_member(user_id=user_id, group_path="/llm")


def test_a_group_larger_than_one_page_comes_back_whole():
    """Keycloak pages member listings; a roster cut at the first page would be
    shown to an administrator as if it were complete."""

    everyone = [{"id": f"u-{n}", "username": f"user{n}", "email": None} for n in range(1203)]

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.url.params.get("first", "0"))
        size = int(request.url.params["max"])
        return httpx.Response(200, json=everyone[first : first + size])

    members = membership(token_or(handler)).list_members("/llm")

    assert [m.id for m in members] == [entry["id"] for entry in everyone]
