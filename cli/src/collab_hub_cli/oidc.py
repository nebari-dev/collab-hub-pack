"""The OAuth side of signing in: discovery, authorization code with PKCE, refresh and sign-out.

The CLI signs in the way the Collab desktop does, against the same realm and
the same public client: the authorization code flow with PKCE (S256) and a
loopback redirect (RFC 8252). A listener on ``127.0.0.1`` and a port the
system picks receives the redirect at ``/callback``, the code is exchanged
with the verifier only this process holds, and ``offline_access`` asks the
realm for a refresh token. Every endpoint comes from the issuer's discovery
document. Signing out revokes the refresh token at the realm's
``revocation_endpoint`` (RFC 7009), and first ends the realm session through
its ``end_session_endpoint`` with that token, as the desktop does.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPES = ("openid", "profile", "email", "offline_access")
LOGIN_TIMEOUT_SECONDS = 300

# Module-level so tests can move time.
clock: Callable[[], float] = time.time


class AuthError(Exception):
    """Not signed in, or the session cannot be used: exit code 5."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float | None


def discover(http: httpx.Client, issuer: str) -> dict:
    try:
        response = http.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    except httpx.HTTPError as exc:
        raise AuthError(f"cannot reach the issuer {issuer}: {exc}") from exc
    if response.status_code != 200:
        raise AuthError(f"the issuer {issuer} has no discovery document (HTTP {response.status_code})")
    return response.json()


def _tokens(body: dict) -> Tokens:
    expires_in = body.get("expires_in")
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=clock() + float(expires_in) if expires_in else None,
    )


def _error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "http_error", f"HTTP {response.status_code}"
    return body.get("error", "http_error"), body.get("error_description") or ""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>collab-hub</title>"
              b"<p>collab-hub: %s You can close this tab.</p>")


class _Callback:
    """The one redirect the loopback listener waits for."""

    def __init__(self, state: str):
        self.state = state
        self.params: dict[str, str] | None = None
        self.arrived = threading.Event()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                if params.get("state") != callback.state:
                    # Not the redirect this sign-in started: never accept a code for another one.
                    self.send_error(400, "state mismatch")
                    return
                callback.params = params
                ok = "code" in params
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_PAGE % (b"signed in." if ok else b"the sign-in did not complete."))
                callback.arrived.set()

            def log_message(self, *_args) -> None:
                pass

        return Handler


def browser_login(
    http: httpx.Client,
    metadata: dict,
    client_id: str,
    open_browser: Callable[[str], object],
    show: Callable[[str], None],
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> Tokens:
    """Sign in through the browser: authorization code with PKCE, redirected to a loopback listener."""

    verifier, challenge = _pkce()
    callback = _Callback(secrets.token_urlsafe(32))
    server = HTTPServer(("127.0.0.1", 0), callback.handler())
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
    url = f"{metadata['authorization_endpoint']}?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": callback.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        show(url)
        open_browser(url)
        if not callback.arrived.wait(timeout):
            raise AuthError(f"no sign-in arrived within {int(timeout)} seconds; run `collab-hub login` again")
    finally:
        server.shutdown()
        server.server_close()

    params = callback.params or {}
    if "code" not in params:
        detail = params.get("error_description") or params.get("error") or "no code"
        raise AuthError(f"the realm did not sign you in: {detail}")
    response = http.post(metadata["token_endpoint"], data={
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    if response.status_code != 200:
        code, detail = _error(response)
        raise AuthError(f"the realm refused the sign-in code: {code} {detail}".rstrip())
    return _tokens(response.json())


def refresh(http: httpx.Client, metadata: dict, client_id: str, refresh_token: str) -> Tokens:
    response = http.post(metadata["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
    })
    if response.status_code != 200:
        code, _ = _error(response)
        raise AuthError(f"the session could not be renewed ({code}); run `collab-hub login` again")
    tokens = _tokens(response.json())
    # A realm that does not rotate refresh tokens omits the new one: keep the old.
    return Tokens(tokens.access_token, tokens.refresh_token or refresh_token, tokens.expires_at)


def sign_out(http: httpx.Client, metadata: dict, client_id: str, refresh_token: str) -> None:
    """Make a refresh token unusable at the realm, and end the realm session behind it.

    Revocation (RFC 7009) is what guarantees the token never renews again, so
    it is required. Ending the session first, through the end-session
    endpoint with the refresh token, is how the desktop signs out of
    Keycloak; it is done when the realm offers it, and a token it already
    ended is still accepted by revocation, which answers 200 for a token that
    is no longer valid.
    """

    revocation = metadata.get("revocation_endpoint")
    if not revocation:
        raise AuthError("the realm publishes no revocation endpoint, so the session cannot be revoked")
    ended = metadata.get("end_session_endpoint")
    if ended:
        response = http.post(ended, data={"client_id": client_id, "refresh_token": refresh_token})
        if response.status_code not in (200, 204):
            code, detail = _error(response)
            raise AuthError(f"the realm did not end the session: {code} {detail}".rstrip())
    response = http.post(revocation, data={
        "client_id": client_id, "token": refresh_token, "token_type_hint": "refresh_token",
    })
    if response.status_code != 200:
        code, detail = _error(response)
        raise AuthError(f"the realm did not revoke the refresh token: {code} {detail}".rstrip())
