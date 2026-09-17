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
