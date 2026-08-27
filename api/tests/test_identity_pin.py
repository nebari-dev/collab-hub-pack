"""Tests for the ACL identity pin (issue #61).

Two halves: the policy itself (which claim becomes the persisted principal, and
what may be written into an ACL), and the access-model regressions the pin must
not disturb — cross-org isolation, deliberate cross-org `public` reads, and the
no-organization path.
"""

from __future__ import annotations

import base64
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.auth import (
    AuthContext,
    DisplayIdentity,
    auth_context_from_claims,
    user_from_claims,
)
from collab_hub_api.frames.identity import (
    IDENTITY_CLAIM_ENV,
    enforce_single_issuer_for_pin,
    identity_pinned_to_sub,
    looks_like_email,
    validate_acl_principal,
)

ALICE_SUB = "a1b2c3d4-1111-4111-8111-abcdefabcdef"
BOB_SUB = "22222222-2222-4222-8222-b0b0b0b0b0b0"
CAROL_SUB = "33333333-3333-4333-8333-cacacacacaca"
# Subjects are opaque: Keycloak mints realm-local UUIDs, composite
# `f:<provider>:<id>` values for federated storage, and operator-defined strings
# for service accounts. All are valid principals under the pin.
FEDERATED_SUB = "f:9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d:jdoe"
SERVICE_ACCOUNT_SUB = "service-account-collab-desktop"
CUSTOM_SUB = "user_01HQ3M9ZR8XK4T"


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(
    sub: str,
    *,
    username: str | None = None,
    email: str | None = None,
    org: str | None = "org-a",
    workspace: str | None = "workspace-a",
) -> dict[str, str]:
    """A signed-off IdToken cookie carrying both principal and display claims.

    ``username`` defaults to the subject id so a test can flip the policy
    mid-run and keep the same principal; tests that care about the difference
    pass a human-shaped username explicitly.
    """

    claims: dict[str, str] = {"sub": sub, "preferred_username": username or sub}
    if email is not None:
        claims["email"] = email
    if org is not None:
        claims["org_id"] = org
    if workspace is not None:
        claims["workspace_id"] = workspace
    return {"IdToken-test": _jwt(claims)}


def _config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )


@pytest_asyncio.fixture
async def pinned_client(tmp_path, monkeypatch):
    """An app started with ACL identity pinned to ``sub`` (the external deployment)."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def legacy_client(tmp_path, monkeypatch):
    """An app started with no identity setting at all — an existing deployment."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def create_frame(client, cookies, *, name="Frame", visibility="private", owners=None):
    payload: dict = {"name": name, "tags": ["team"], "body": "# Body", "visibility": visibility}
    if owners is not None:
        payload["owners"] = owners
    return await client.post("/v1/frames", cookies=cookies, json=payload)


async def create_published_frame(client, cookies, *, visibility: str):
    response = await create_frame(client, cookies, visibility=visibility)
    assert response.status_code == 201, response.text
    frame = response.json()
    published = await client.post(f"/v1/frames/{frame['id']}/publish", cookies=cookies)
    assert published.status_code == 200
    return frame


# --------------------------------------------------------------------------
# Policy: which claim becomes the persisted principal
# --------------------------------------------------------------------------


def test_unset_identity_claim_keeps_legacy_precedence(monkeypatch):
    # The default must not change behavior for a deployment that already stores
    # username/email principals: flipping it would orphan every stored ACL.
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    assert identity_pinned_to_sub() is False
    assert user_from_claims({"preferred_username": "alice", "email": "alice@example.com", "sub": ALICE_SUB}) == "alice"


def test_explicit_legacy_keeps_legacy_precedence(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "legacy")
    assert identity_pinned_to_sub() is False
    assert user_from_claims({"email": "alice@example.com", "sub": ALICE_SUB}) == "alice@example.com"


def test_pin_uses_the_sub_claim_only(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    assert identity_pinned_to_sub() is True
    claims = {"preferred_username": "alice", "email": "alice@example.com", "sub": ALICE_SUB}
    assert user_from_claims(claims) == ALICE_SUB


def test_pin_refuses_a_token_without_a_sub(monkeypatch):
    # No falling back to the mutable claims: a token with no subject is simply
    # not an identity this deployment can persist.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    assert user_from_claims({"preferred_username": "alice", "email": "alice@example.com"}) is None


@pytest.mark.parametrize("value", ["Legacy", "SUB", " sub ", "subject", "true", "email"])
def test_unrecognized_identity_claim_values_fail_loudly(monkeypatch, value):
    # Exact match only. Guessing is wrong in both directions, and normalizing a
    # spelling would silently accept an operator's assumption about parsing.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, value)
    with pytest.raises(RuntimeError, match=IDENTITY_CLAIM_ENV):
        identity_pinned_to_sub()


def test_startup_fails_on_an_unrecognized_identity_claim(tmp_path, monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "Sub")
    with pytest.raises(RuntimeError, match=IDENTITY_CLAIM_ENV):
        make_app(_config(tmp_path))


# --------------------------------------------------------------------------
# Display fields are structurally separate from the principal
# --------------------------------------------------------------------------


def test_display_claims_are_captured_but_are_not_the_principal(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    auth = auth_context_from_claims(
        {
            "sub": ALICE_SUB,
            "preferred_username": "alice",
            "name": "Alice Example",
            "email": "alice@example.com",
            "org_id": "org-a",
            "workspace_id": "workspace-a",
        }
    )
    assert auth is not None
    assert auth.user == ALICE_SUB
    assert auth.display == DisplayIdentity(name="Alice Example", email="alice@example.com")
    # Usage reporting still reads the email, through a read-only view.
    assert auth.email == "alice@example.com"


def test_an_email_cannot_be_constructed_into_the_principal_tier():
    # Structural, not conventional: display strings live on DisplayIdentity, so
    # there is no `email=` peer of `user` for an access check to reach for.
    with pytest.raises(TypeError):
        AuthContext(user=ALICE_SUB, home_org_id="org-a", workspace_id="workspace-a", email="alice@example.com")
    auth = AuthContext(user=ALICE_SUB, home_org_id="org-a", workspace_id="workspace-a")
    assert auth.email is None


def test_legacy_mode_still_captures_display_fields(monkeypatch):
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    auth = auth_context_from_claims(
        {
            "preferred_username": "alice",
            "email": "alice@example.com",
            "org_id": "org-a",
            "workspace_id": "workspace-a",
        }
    )
    assert auth is not None
    assert auth.user == "alice"
    assert auth.email == "alice@example.com"
    assert auth.display.name == "alice"


# --------------------------------------------------------------------------
# Single-issuer assumption (R12) — recorded in frames/identity.py, enforced here
# --------------------------------------------------------------------------


def test_single_issuer_check_is_a_no_op_under_legacy(monkeypatch):
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    enforce_single_issuer_for_pin()


def test_pin_without_a_configured_verifier_passes_vacuously(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    enforce_single_issuer_for_pin()


def test_pin_requires_the_bearer_issuer_to_be_named(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="FRAMES_BEARER_ISSUER"):
        enforce_single_issuer_for_pin()


def test_pin_accepts_the_idtoken_issuer_falling_back_to_bearer(monkeypatch):
    # Mirrors decode_id_token_payload's own fallback.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://a.example.com/realms/apollo")
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.delenv("FRAMES_IDTOKEN_ISSUER", raising=False)
    enforce_single_issuer_for_pin()


def test_pin_rejects_an_idtoken_issuer_riding_the_shared_bearer_jwks(monkeypatch):
    # The IdToken verifier exists whenever *either* JWKS URL is set: the decode
    # path falls back to the bearer URL. Naming a different IdToken issuer while
    # reusing that key set means cookies are trusted under issuer B while bearer
    # tokens use issuer A — a second trusted issuer with no second JWKS URL.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://shared.example/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://issuer-a")
    monkeypatch.setenv("FRAMES_IDTOKEN_ISSUER", "https://issuer-b")
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    with pytest.raises(RuntimeError, match="single trusted issuer"):
        enforce_single_issuer_for_pin()


def test_startup_rejects_an_idtoken_issuer_riding_the_shared_bearer_jwks(tmp_path, monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://shared.example/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://issuer-a")
    monkeypatch.setenv("FRAMES_IDTOKEN_ISSUER", "https://issuer-b")
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    with pytest.raises(RuntimeError, match="single trusted issuer"):
        make_app(_config(tmp_path))


def test_pin_accepts_one_issuer_across_both_verifiers(monkeypatch):
    # The same fallback in its benign form: no IdToken settings at all, so the
    # cookie verifier simply *is* the bearer verifier.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://shared.example/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://issuer-a")
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_ISSUER", raising=False)
    enforce_single_issuer_for_pin()


def test_pin_rejects_two_trusted_issuers(monkeypatch):
    # A 'sub' minted by issuer B would otherwise BE issuer A's user.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://a.example.com/realms/apollo")
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", "https://b.example.com/jwks")
    monkeypatch.setenv("FRAMES_IDTOKEN_ISSUER", "https://b.example.com/realms/other")
    with pytest.raises(RuntimeError, match="single trusted issuer"):
        enforce_single_issuer_for_pin()


def test_startup_rejects_two_trusted_issuers_when_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://a.example.com/jwks")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://a.example.com/realms/apollo")
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", "https://b.example.com/jwks")
    monkeypatch.setenv("FRAMES_IDTOKEN_ISSUER", "https://b.example.com/realms/other")
    with pytest.raises(RuntimeError, match="single trusted issuer"):
        make_app(_config(tmp_path))


# --------------------------------------------------------------------------
# Principal validation (version-skew flag S1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["alice@example.com", "Alice.Example@corp.example.co.uk", "alice+frames@example.com"],
)
def test_email_principals_are_rejected_when_pinned(monkeypatch, value):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    assert looks_like_email(value) is True
    with pytest.raises(ValueError, match="email address"):
        validate_acl_principal(value)


@pytest.mark.parametrize(
    "value",
    [ALICE_SUB, FEDERATED_SUB, SERVICE_ACCOUNT_SUB, CUSTOM_SUB, ALICE_SUB.upper()],
)
def test_opaque_subjects_of_any_format_are_accepted_when_pinned(monkeypatch, value):
    # Identity semantics are never inferred from syntax: a token carrying any of
    # these authenticates and has its subject persisted as `created_by`, so
    # refusing them as *grants* would reject the very values this deployment
    # writes as *owners* (and the ids the user directory feeds the picker).
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    assert looks_like_email(value) is False
    assert validate_acl_principal(value) == value


def test_principal_validation_is_a_pass_through_under_legacy(monkeypatch):
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    assert validate_acl_principal("alice@example.com") == "alice@example.com"


# --------------------------------------------------------------------------
# HTTP: grants written as emails are rejected, never silently stored
# --------------------------------------------------------------------------


async def test_email_shaped_owner_grants_are_rejected(pinned_client):
    alice = auth_cookie(ALICE_SUB)
    created = await create_frame(pinned_client, alice)
    assert created.status_code == 201
    frame_id = created.json()["id"]
    assert created.json()["owners"] == [ALICE_SUB]
    assert created.json()["created_by"] == ALICE_SUB

    add = await pinned_client.post(
        f"/v1/frames/{frame_id}/owners",
        cookies=alice,
        json={"email": "bob@example.com"},
    )
    assert add.status_code == 422
    replace = await pinned_client.put(
        f"/v1/frames/{frame_id}/owners",
        cookies=alice,
        json={"owners": [ALICE_SUB, "bob@example.com"]},
    )
    assert replace.status_code == 422

    # The dangerous outcome is a grant that looks stored and matches nobody, so
    # assert the list is untouched rather than only the status code.
    owners = await pinned_client.get(f"/v1/frames/{frame_id}/owners", cookies=alice)
    assert owners.json() == {"owners": [ALICE_SUB]}


async def test_email_shaped_reader_grants_are_rejected(pinned_client):
    alice = auth_cookie(ALICE_SUB)
    created = await create_frame(pinned_client, alice)
    frame_id = created.json()["id"]

    add = await pinned_client.post(
        f"/v1/frames/{frame_id}/readers",
        cookies=alice,
        json={"email": "bob@example.com"},
    )
    assert add.status_code == 422
    replace = await pinned_client.put(
        f"/v1/frames/{frame_id}/readers",
        cookies=alice,
        json={"readers": ["bob@example.com"]},
    )
    assert replace.status_code == 422

    readers = await pinned_client.get(f"/v1/frames/{frame_id}/readers", cookies=alice)
    assert readers.json() == {"readers": []}


async def test_email_shaped_owner_seeds_are_rejected_at_create(pinned_client):
    created = await create_frame(pinned_client, auth_cookie(ALICE_SUB), owners=["bob@example.com"])
    assert created.status_code == 422


async def test_email_shaped_group_owner_grants_are_rejected(pinned_client):
    alice = auth_cookie(ALICE_SUB)
    frame = await create_frame(pinned_client, alice)
    group = await pinned_client.post(
        "/v1/frame-groups",
        cookies=alice,
        json={"name": "Group", "frame_ids": [frame.json()["id"]]},
    )
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    assert group.json()["owners"] == [ALICE_SUB]

    add = await pinned_client.post(
        f"/v1/frame-groups/{group_id}/owners",
        cookies=alice,
        json={"email": "bob@example.com"},
    )
    assert add.status_code == 422
    replace = await pinned_client.put(
        f"/v1/frame-groups/{group_id}/owners",
        cookies=alice,
        json={"owners": [ALICE_SUB, "bob@example.com"]},
    )
    assert replace.status_code == 422

    owners = await pinned_client.get(f"/v1/frame-groups/{group_id}/owners", cookies=alice)
    assert owners.json() == {"owners": [ALICE_SUB]}


async def test_sub_shaped_grants_still_work_when_pinned(pinned_client):
    """Positive control: the pin narrows what may be granted, not whether it works."""

    alice = auth_cookie(ALICE_SUB)
    bob = auth_cookie(BOB_SUB)
    created = await create_published_frame(pinned_client, alice, visibility="private")
    frame_id = created["id"]

    assert (await pinned_client.get(f"/v1/frames/{frame_id}", cookies=bob)).status_code == 404
    granted = await pinned_client.post(
        f"/v1/frames/{frame_id}/readers",
        cookies=alice,
        json={"email": BOB_SUB},
    )
    assert granted.status_code == 200
    assert granted.json() == {"readers": [BOB_SUB]}
    assert (await pinned_client.get(f"/v1/frames/{frame_id}", cookies=bob)).status_code == 200


@pytest.mark.parametrize("grantee", [FEDERATED_SUB, SERVICE_ACCOUNT_SUB, CUSTOM_SUB])
async def test_non_uuid_subjects_can_be_granted_and_then_read(pinned_client, grantee):
    """Federated, service-account, and custom subjects are first-class principals.

    Each of these is a subject Keycloak (or an operator) legitimately mints, and
    a token carrying one authenticates here — so the grant must succeed and the
    grantee must actually gain access, not merely be stored.
    """

    alice = auth_cookie(ALICE_SUB)
    frame = await create_published_frame(pinned_client, alice, visibility="private")
    frame_id = frame["id"]

    granted = await pinned_client.post(
        f"/v1/frames/{frame_id}/readers",
        cookies=alice,
        json={"email": grantee},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json() == {"readers": [grantee]}

    read = await pinned_client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie(grantee))
    assert read.status_code == 200

    seeded = await create_frame(pinned_client, alice, owners=[grantee])
    assert seeded.status_code == 201
    assert seeded.json()["owners"] == [ALICE_SUB, grantee]


async def test_legacy_deployments_still_accept_email_grants(legacy_client):
    """An existing deployment is untouched by default — grants and identities alike."""

    alice = auth_cookie(ALICE_SUB, username="alice")
    created = await create_frame(legacy_client, alice)
    assert created.status_code == 201
    assert created.json()["owners"] == ["alice"]

    added = await legacy_client.post(
        f"/v1/frames/{created.json()['id']}/readers",
        cookies=alice,
        json={"email": "bob@example.com"},
    )
    assert added.status_code == 200
    assert added.json() == {"readers": ["bob@example.com"]}


async def test_pre_pin_principals_still_load_and_can_be_removed(tmp_path, monkeypatch):
    """Records written before the pin must stay readable, and repairable.

    Validation is on the *write* path only, so an email principal stored under
    the old policy still loads afterwards and the removal route still deletes
    it — otherwise a deployment that flipped the setting could neither see nor
    clean up its own data.
    """

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    app = make_app(_config(tmp_path))
    # The username equals the subject id, so the same principal owns the frame
    # on both sides of the flip; only the grant policy changes.
    alice = auth_cookie(ALICE_SUB)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await create_frame(client, alice)
            frame_id = created.json()["id"]
            legacy_grant = await client.post(
                f"/v1/frames/{frame_id}/readers",
                cookies=alice,
                json={"email": "bob@example.com"},
            )
            assert legacy_grant.json() == {"readers": ["bob@example.com"]}

            monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")

            still_there = await client.get(f"/v1/frames/{frame_id}/readers", cookies=alice)
            assert still_there.status_code == 200
            assert still_there.json() == {"readers": ["bob@example.com"]}

            removed = await client.delete(
                f"/v1/frames/{frame_id}/readers/bob@example.com",
                cookies=alice,
            )
            assert removed.status_code == 200
            assert removed.json() == {"readers": []}


# --------------------------------------------------------------------------
# Access-model regressions the pin must not disturb
# --------------------------------------------------------------------------


async def test_internal_frames_are_not_readable_across_orgs(pinned_client):
    alice_org_a = auth_cookie(ALICE_SUB, org="org-a")
    bob_org_b = auth_cookie(BOB_SUB, org="org-b")
    frame = await create_published_frame(pinned_client, bob_org_b, visibility="internal")

    # 404, never 403: existence is not leaked across orgs.
    detail = await pinned_client.get(f"/v1/frames/{frame['id']}", cookies=alice_org_a)
    assert detail.status_code == 404
    listing = await pinned_client.get("/v1/frames", cookies=alice_org_a)
    assert listing.json() == []


async def test_public_frames_are_readable_across_orgs(pinned_client):
    """INTENTIONAL: `public` reads cross-org. This is the product behavior, not a bug.

    `public` is the one tier that deliberately crosses the tenant boundary — the
    access model documents it (`can_read` drops the scope check for `public`
    alone). This test exists so that a future reader who mistakes it for a
    cross-org leak has to change a test that says, in words, that it is not one.
    Management stays tenant-scoped: a cross-org caller reads, and cannot mutate.
    """

    alice_org_a = auth_cookie(ALICE_SUB, org="org-a")
    bob_org_b = auth_cookie(BOB_SUB, org="org-b")
    frame = await create_published_frame(pinned_client, bob_org_b, visibility="public")

    detail = await pinned_client.get(f"/v1/frames/{frame['id']}", cookies=alice_org_a)
    assert detail.status_code == 200
    assert detail.json()["id"] == frame["id"]

    # ...but only reads: cross-org management is still refused.
    mutation = await pinned_client.put(
        f"/v1/frames/{frame['id']}/owners",
        cookies=alice_org_a,
        json={"owners": [ALICE_SUB]},
    )
    assert mutation.status_code == 403

    # Discovery is not part of `public`: the list stays scoped to the caller's org.
    listing = await pinned_client.get("/v1/frames", cookies=alice_org_a)
    assert listing.json() == []


async def test_a_caller_with_no_organization_gets_no_access(pinned_client):
    """The no-organization path: a valid identity with no org resolves to no access.

    Today an org-less token simply fails to produce an auth context, so every
    frames route answers 401 and the caller reaches nothing — which is the
    property that matters here. Turning that into the distinct `no_organization`
    code (so clients stop rendering it as "sign in again") is the auth
    choke-point work, issue #63, not this change.
    """

    # Only the org claim is missing, so the 401 can only be attributed to it.
    orgless = auth_cookie(CAROL_SUB, org=None, workspace="workspace-a")
    assert (await pinned_client.get("/v1/frames", cookies=orgless)).status_code == 401
    created = await create_frame(pinned_client, orgless)
    assert created.status_code == 401

    # And it is the missing org, not the missing subject: the same token with an
    # org attached authenticates fine.
    with_org = auth_cookie(CAROL_SUB)
    assert (await pinned_client.get("/v1/frames", cookies=with_org)).status_code == 200


async def test_a_caller_with_no_workspace_gets_no_access(pinned_client):
    """The workspace half of the same scope requirement, isolated from the org half."""

    workspaceless = auth_cookie(CAROL_SUB, org="org-a", workspace=None)
    assert (await pinned_client.get("/v1/frames", cookies=workspaceless)).status_code == 401


async def test_a_token_without_a_sub_is_unauthenticated_when_pinned(pinned_client):
    token = _jwt(
        {
            "preferred_username": "alice",
            "email": "alice@example.com",
            "org_id": "org-a",
            "workspace_id": "workspace-a",
        }
    )
    response = await pinned_client.get("/v1/frames", cookies={"IdToken-test": token})
    assert response.status_code == 401
