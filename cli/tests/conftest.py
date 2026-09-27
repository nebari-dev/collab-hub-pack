"""A stub realm and a stub hub behind one ``httpx.MockTransport``, and a browser that follows the redirect."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from collab_hub_cli import hub, main, oidc

HUB = "http://hub.test"
ISSUER = "https://id.test/realms/nebari"
CLIENT = "apollo-desktop"


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


@dataclass
class Stub:
    """The realm and the hub, and a record of what was asked of them."""

    dev_auth: bool = False
    issuer: str | None = ISSUER
    users: dict[str, str] = field(default_factory=dict)  # access token -> user
    refreshable: dict[str, str] = field(default_factory=dict)  # refresh token -> user
    requests: list[httpx.Request] = field(default_factory=list)
    authorizations: list[dict] = field(default_factory=list)
    exchanges: list[dict] = field(default_factory=list)
    ended: list[dict] = field(default_factory=list)
    cogs: list[dict] = field(default_factory=list)
    counter: int = 0

    def issue(self, user: str) -> dict:
        self.counter += 1
        access, refresh = f"access-{self.counter}", f"refresh-{self.counter}"
        self.users[access], self.refreshable[refresh] = user, user
        return {"access_token": access, "refresh_token": refresh, "expires_in": 300, "token_type": "Bearer"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}
        base = f"{url.scheme}://{url.host}"
        if base == "https://id.test":
            return self._realm(url.path, form)
        if base == HUB:
            return self._hub(request)
        return httpx.Response(599)

    def _realm(self, path: str, form: dict) -> httpx.Response:
        realm = "/realms/nebari/protocol/openid-connect"
        if path == "/realms/nebari/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
                "revocation_endpoint": f"{ISSUER}/protocol/openid-connect/revoke",
            })
        if path == f"{realm}/token" and form.get("grant_type") == "authorization_code":
            self.exchanges.append(form)
            [asked] = [a for a in self.authorizations if a["code"] == form["code"]]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
            if (challenge.decode().rstrip("=") != asked["code_challenge"]
                    or form["redirect_uri"] != asked["redirect_uri"] or form["client_id"] != CLIENT):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self.issue("alice"))
        if path == f"{realm}/token" and form.get("grant_type") == "refresh_token":
            user = self.refreshable.pop(form["refresh_token"], None)
            if user is None:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token is not active"})
            return httpx.Response(200, json=self.issue(user))
        if path == f"{realm}/logout":
            self.ended.append(form)
            self.refreshable.pop(form.get("refresh_token"), None)
            return httpx.Response(204)
        return httpx.Response(404)

    def _hub(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/auth/cli":
            return httpx.Response(200, json={"issuer": self.issuer, "client_id": CLIENT, "dev_auth": self.dev_auth})
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ") if header else None
        user = self.users.get(token) if token else ("dev-user" if self.dev_auth else None)
        if user is None:
            return httpx.Response(401, json={"detail": "Invalid bearer token" if token else "Authentication required"})
        if path == "/v1/me":
            return httpx.Response(200, json={
                "user": user, "org_id": "org-a", "workspace_id": "default", "org_role": "owner",
                "platform_role": None, "name": None, "email": None,
                "authenticated_by": "token" if token else "dev",
            })
        if path == "/v1/cogs":
            limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
            items = [c for c in self.cogs if request.url.params.get("kind") in (None, c["card"].get("kind"))]
            page = items[offset:offset + limit]
            more = offset + limit < len(items)
            return httpx.Response(200, json={"items": page, "limit": limit, "offset": offset,
                                             "next_offset": offset + limit if more else None})
        if path.startswith("/v1/cogs/"):
            cog_id = path.removeprefix("/v1/cogs/")
            for cog in self.cogs:
                if cog["cog_id"] == cog_id:
                    return httpx.Response(200, json={**cog, "versions": [cog]})
            return httpx.Response(404, json={"error": {"code": "cog_not_found", "message": f"No Cog {cog_id}"}})
        return httpx.Response(404)

    def browser(self, approve: bool = True):
        """What a browser does with the sign-in URL: the realm signs the user in and redirects back."""

        def open_url(url: str) -> bool:
            query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            code = f"code-{len(self.authorizations)}"
            self.authorizations.append({**query, "code": code})
            answer = {"code": code} if approve else {"error": "access_denied", "error_description": "denied"}
            try:
                urllib.request.urlopen(f"{query['redirect_uri']}?{urlencode({**answer, 'state': query['state']})}")
            except urllib.error.HTTPError:
                pass
            return True

        return open_url


@pytest.fixture
def stub(monkeypatch, tmp_path) -> Stub:
    stub = Stub()
    monkeypatch.setattr(hub, "transport", httpx.MockTransport(stub.handle))
    monkeypatch.setattr(main.webbrowser, "open", stub.browser())
    monkeypatch.setenv("COLLAB_HUB_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("COLLAB_HUB_URL", "COLLAB_HUB_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_000.0)
    return stub


@pytest.fixture
def cli():
    runner = CliRunner()

    def invoke(*args: str, input: str | None = None):
        return runner.invoke(main.app, list(args), input=input)

    return invoke
