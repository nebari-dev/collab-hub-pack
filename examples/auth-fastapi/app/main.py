"""
Auth-aware FastAPI application for Nebari.

Demonstrates how to read authenticated user identity from the IdToken cookie
set by Envoy Gateway after Keycloak OIDC authentication.

When deployed on Nebari with auth enabled, the Envoy Gateway OIDC filter
handles the login flow and sets an IdToken cookie containing the JWT.
This app reads that cookie to display user information.
"""

import base64
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Auth-Aware Nebari Pack")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def decode_jwt_payload(token: str) -> dict:
    """Decode the payload section of a JWT without verifying the signature.

    This is safe because the token was already verified by Envoy Gateway.
    We only need to extract the claims for display purposes.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}

    # JWT payload is base64url-encoded
    payload = parts[1]
    # Add padding if needed
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding

    try:
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}


def get_id_token(request: Request) -> str | None:
    """Extract the IdToken from Envoy Gateway's OIDC filter cookies.

    Envoy Gateway's OIDC filter sets a cookie named IdToken-<suffix> where
    <suffix> is an 8-character hex string derived from the SecurityPolicy UID.
    For example: IdToken-a1b2c3d4

    We look for any cookie starting with "IdToken-" to avoid hardcoding
    the suffix value.
    """
    for name, value in request.cookies.items():
        if name.startswith("IdToken-"):
            return value

    return None


@app.get("/health")
def health():
    """Health check endpoint for Kubernetes probes."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Main page showing authenticated user information."""
    token = get_id_token(request)
    user_info = None

    if token:
        claims = decode_jwt_payload(token)
        user_info = {
            "username": claims.get("preferred_username", "unknown"),
            "email": claims.get("email", ""),
            "name": claims.get("name", ""),
            "groups": claims.get("groups", []),
            "roles": claims.get("realm_access", {}).get("roles", []),
        }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_info": user_info,
            "authenticated": user_info is not None,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
