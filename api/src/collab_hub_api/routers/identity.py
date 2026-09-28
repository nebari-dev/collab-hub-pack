"""Who is calling, and how a terminal client signs in: the routes the ``collab-hub`` CLI starts from.

- ``GET /v1/auth/cli`` -- public. The issuer and the client id a command-line
  client signs in with, so a user only ever types the hub's URL. The client is
  the realm's public client the desktop already signs in with (authorization
  code with PKCE on a loopback redirect), so the CLI needs no realm client of
  its own and its tokens carry the audience the hub already checks. It also
  says when the hub runs the dev-auth shortcut, where there is no realm to
  sign in to.
- ``GET /v1/me`` -- authenticated. What the hub resolved for the caller: the
  principal, the organization and the roles it will act on, and how the caller
  was authenticated. A client reports this rather than decoding its own token,
  so it says what the hub sees, not what the token claims.

Neither route is a second authentication path: ``/v1/me`` is answered through
:func:`~..frames.auth.get_auth_context` like every other API route, and
``/v1/auth/cli`` names only public facts the realm's own discovery document
publishes.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..frames.auth import AuthContext, _credential_claims, _dev_auth_user, get_auth_context

CLI_CLIENT_ID_ENV = "COLLAB_HUB_CLI_CLIENT_ID"
DEFAULT_CLI_CLIENT_ID = "apollo-desktop"
"""The desktop's public client: PKCE, loopback redirects, and the audience ``FRAMES_BEARER_AUDIENCE`` checks."""
BEARER_ISSUER_ENV = "FRAMES_BEARER_ISSUER"

router = APIRouter(tags=["identity"])

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]


class CliAuthConfig(BaseModel):
    """How a command-line client signs in to this hub."""

    issuer: str | None = Field(
        description="The OpenID issuer to sign in with, whose discovery document names the authorization, "
        "token, end-session and revocation endpoints the sign-in and sign-out use. Null when the hub "
        "verifies no bearer tokens."
    )
    client_id: str = Field(
        description="The public realm client to sign in with: authorization code with PKCE (S256) on a "
        "loopback redirect, http://127.0.0.1:<port>/callback."
    )
    dev_auth: bool = Field(
        description="True when the hub answers requests without credentials as a fixed development "
        "user. Nobody is signed in then, whatever a client holds."
    )


class Me(BaseModel):
    """The caller as the hub resolved them."""

    user: str = Field(description="The principal every access check compares against.")
    org_id: str | None = Field(description="The caller's home organization; null for an operator with none.")
    workspace_id: str
    org_role: str | None = None
    platform_role: str | None = None
    name: str | None = Field(default=None, description="Display only, never a principal.")
    email: str | None = Field(default=None, description="Display only, never a principal.")
    authenticated_by: Literal["token", "dev"] = Field(
        description="`token` when the request carried a credential the hub verified; `dev` when the "
        "dev-auth shortcut answered for a request that carried none."
    )


@router.get("/auth/cli", response_model=CliAuthConfig, summary="How a command-line client signs in")
def cli_auth_config() -> CliAuthConfig:
    issuer = os.environ.get(BEARER_ISSUER_ENV, "").strip().rstrip("/") or None
    client_id = os.environ.get(CLI_CLIENT_ID_ENV, "").strip() or DEFAULT_CLI_CLIENT_ID
    return CliAuthConfig(issuer=issuer, client_id=client_id, dev_auth=_dev_auth_user() is not None)


@router.get("/me", response_model=Me, summary="The caller as the hub resolved them")
def me(request: Request, auth: AuthDep) -> Me:
    # The resolver's own test: get_auth_context authenticates from a credential
    # whenever _credential_claims finds one (a non-empty Bearer token or IdToken
    # cookie), and 401s if it does not verify; only when it finds none does the
    # dev shortcut answer. A header it does not read, such as Basic, or an empty
    # Bearer, is no credential to it, so the answer is `dev` then, not `token`.
    presented = _credential_claims(request) is not None
    return Me(
        user=auth.user,
        org_id=auth.home_org_id,
        workspace_id=auth.workspace_id,
        org_role=auth.org_role,
        platform_role=auth.platform_role,
        name=auth.display.name,
        email=auth.display.email,
        authenticated_by="token" if presented else "dev",
    )
