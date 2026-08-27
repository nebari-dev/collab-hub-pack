"""Authentication helpers for gateway-validated Nebari requests."""

from __future__ import annotations

import base64
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

usage_logger = logging.getLogger("frames_server.usage")


@dataclass(frozen=True)
class AuthContext:
    """Authenticated caller identity and workspace scope."""

    user: str
    org_id: str
    workspace_id: str
    email: str | None = None
    """The caller's email claim, when present. Captured for usage reporting
    only — ``user`` remains the stable owner identity everywhere else."""


current_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "frames_server_current_auth",
    default=None,
)


class TokenDecodeError(ValueError):
    """Raised when an auth token cannot be decoded or verified."""

    pass


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}

    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding

    try:
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}


def unsafe_auth_enabled() -> bool:
    """Return whether local/test-only auth shortcuts may be used."""

    return os.environ.get("FRAMES_UNSAFE_AUTH_ENABLED") == "true"


def decode_bearer_payload(token: str) -> dict:
    """Verify and decode a native-client bearer token.

    Gateway cookies are already verified by Envoy before they reach the app.
    Bearer tokens can be supplied by native clients, so they need an explicit
    verification path unless tests opt into unsigned tokens.
    """

    if unsafe_auth_enabled() and os.environ.get("FRAMES_BEARER_ALLOW_UNSIGNED") == "true":
        return decode_jwt_payload(token)

    return decode_verified_jwt(
        token,
        jwks_url=os.environ.get("FRAMES_BEARER_JWKS_URL"),
        issuer=os.environ.get("FRAMES_BEARER_ISSUER"),
        audience=os.environ.get("FRAMES_BEARER_AUDIENCE"),
    )


def decode_id_token_payload(token: str) -> dict:
    """Verify and decode a gateway/native IdToken cookie.

    The gateway normally verifies IdToken cookies before forwarding browser
    traffic, but some routes intentionally bypass gateway auth so native clients
    can use bearer tokens. Since those routes can still receive caller-supplied
    cookies, the app verifies cookie signatures before trusting claims.
    """

    if unsafe_auth_enabled() and os.environ.get("FRAMES_IDTOKEN_ALLOW_UNSIGNED") == "true":
        return decode_jwt_payload(token)

    return decode_verified_jwt(
        token,
        jwks_url=os.environ.get("FRAMES_IDTOKEN_JWKS_URL") or os.environ.get("FRAMES_BEARER_JWKS_URL"),
        issuer=os.environ.get("FRAMES_IDTOKEN_ISSUER") or os.environ.get("FRAMES_BEARER_ISSUER"),
        audience=os.environ.get("FRAMES_IDTOKEN_AUDIENCE"),
    )


def decode_verified_jwt(token: str, *, jwks_url: str | None, issuer: str | None, audience: str | None) -> dict:
    """Verify a JWT signature and decode its payload."""

    if not jwks_url:
        raise TokenDecodeError("JWT verification is not configured")

    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:
        raise TokenDecodeError("JWT verification dependencies are missing") from exc

    decode_options = {} if audience else {"verify_aud": False}

    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options=decode_options,
        )
    except Exception as exc:
        raise TokenDecodeError("Invalid JWT") from exc


def get_id_token(request: Request) -> str | None:
    """Return the gateway-set IdToken cookie when present."""

    for name, value in request.cookies.items():
        if name.startswith("IdToken-"):
            return value
    return None


def get_bearer_token(request: Request) -> str | None:
    """Return a Nebari-issued bearer token when native clients provide one."""

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return None


def user_from_claims(claims: dict) -> str | None:
    """Choose the stable owner identity from accepted JWT claims."""

    for key in ("preferred_username", "email", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _claim_value(claims: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def auth_context_from_claims(claims: dict) -> AuthContext | None:
    """Build workspace-scoped auth context from accepted JWT claims."""

    user = user_from_claims(claims)
    if not user:
        return None
    org_id = _claim_value(claims, ("org_id", "organization_id", "org")) or _env_value("FRAMES_AUTH_DEFAULT_ORG")
    workspace_id = _claim_value(claims, ("workspace_id", "workspace")) or _env_value(
        "FRAMES_AUTH_DEFAULT_WORKSPACE"
    )
    if not org_id or not workspace_id:
        return None
    email = _claim_value(claims, ("email",))
    return AuthContext(user=user, org_id=org_id, workspace_id=workspace_id, email=email)


def record_user_seen(request: Request, auth_context: AuthContext) -> AuthContext:
    """Best-effort usage capture of an authenticated caller.

    Reads the usage store off the app owning the request (the FastAPI app for
    HTTP routes, the mounted MCP app for MCP traffic) so nothing here holds
    global state. Never raises: a usage-write failure must not break auth.
    """

    state = getattr(request.app, "state", None)
    usage_store = getattr(state, "usage_store", None)
    if usage_store is None:
        return auth_context
    try:
        usage_store.record_user_seen(
            auth_context.org_id,
            auth_context.workspace_id,
            auth_context.user,
            auth_context.email,
        )
    except Exception:
        from .observability import USAGE_WRITE_FAILURES

        USAGE_WRITE_FAILURES.labels(kind="user_seen").inc()
        usage_logger.exception(
            "usage_user_seen_write_failed",
            extra={"user": auth_context.user},
        )
    return auth_context


def get_auth_context(request: Request) -> AuthContext:
    """Resolve authenticated caller and workspace scope from gateway or dev auth."""

    id_token = get_id_token(request)
    if id_token:
        try:
            claims = decode_id_token_payload(id_token)
        except TokenDecodeError:
            claims = {}
        auth_context = auth_context_from_claims(claims)
        if auth_context:
            return record_user_seen(request, auth_context)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid IdToken cookie",
        )

    bearer_token = get_bearer_token(request)
    if bearer_token:
        try:
            claims = decode_bearer_payload(bearer_token)
        except TokenDecodeError:
            claims = {}
        auth_context = auth_context_from_claims(claims)
        if auth_context:
            return record_user_seen(request, auth_context)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )

    dev_user = os.environ.get("DEV_AUTH_USER")
    if unsafe_auth_enabled() and os.environ.get("DEV_AUTH_ENABLED") == "true" and dev_user:
        return record_user_seen(
            request,
            AuthContext(
                user=dev_user,
                org_id=os.environ.get("DEV_AUTH_ORG", "dev-org"),
                workspace_id=os.environ.get("DEV_AUTH_WORKSPACE", "default"),
            ),
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def get_current_user(request: Request) -> str:
    """Resolve the authenticated user id for legacy call sites."""

    return get_auth_context(request).user
