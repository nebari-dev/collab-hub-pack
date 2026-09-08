from __future__ import annotations

import logging

import httpx
from fastapi import Request

from collab_hub_api.config import NotionConnectorConfig
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorPermissionError,
    ConnectorReconnectRequired,
    ConnectorTokenError,
)
from collab_hub_api.frames.auth import get_bearer_token

logger = logging.getLogger("frames_server.connectors")


class NotionTokenProvider:
    """Resolve a per-user Notion workspace bot token.

    Notion is not an OIDC provider: it issues a non-expiring, workspace-scoped
    bot token with no refresh token. Under Option A the token is brokered through
    a Keycloak generic-OAuth identity provider (this class, near-identical to
    ``GitHubTokenProvider``). Under Option B the broker call is replaced by a read
    from the Hub's own encrypted per-user token store, keyed by the Hub identity
    in ``request`` -- the exceptions and return type stay the same, so nothing
    below the provider changes. See docs/notion-connector.md (Option A vs B).
    """

    def __init__(self, config: NotionConnectorConfig):
        self.config = config

    async def access_token(self, request: Request) -> str:
        if self.config.static_access_token:
            return self.config.static_access_token  # dev/CI escape hatch

        if not self.config.broker_token_url:
            raise ConnectorNotConnected("Notion connector token broker is not configured")

        hub_token = get_bearer_token(request)
        if not hub_token:
            raise ConnectorReconnectRequired("Hub bearer token is required for Notion connector token brokering")

        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.config.broker_token_url,
                    headers={"Authorization": f"Bearer {hub_token}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ConnectorTokenError("Notion token broker request failed") from exc

        logger.info("notion broker token response status=%s", response.status_code)

        if response.status_code == 404:
            raise ConnectorNotConnected("Notion workspace is not linked")
        if response.status_code == 403:
            raise ConnectorPermissionError(
                "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
            )
        if response.status_code in {400, 401}:
            raise ConnectorReconnectRequired("Notion workspace must be reconnected")
        if response.status_code >= 400:
            raise ConnectorTokenError("Notion token broker request failed")

        token = _extract_access_token(response)
        if not token:
            raise ConnectorReconnectRequired("Notion token broker did not return an access token")
        return token


def _extract_access_token(response: httpx.Response) -> str:
    """Pull the workspace bot token from a Keycloak broker token response.

    Notion's OAuth token endpoint returns JSON with the bot token under
    ``access_token`` (alongside ``bot_id``/``workspace_id``/``workspace_name``);
    Keycloak brokers that JSON back. Only the token is read here -- the workspace
    fields are surfaced later by the capability probe, never from this payload.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
        if isinstance(token, str) and token:
            return token
    return ""
