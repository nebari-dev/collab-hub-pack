from __future__ import annotations

import logging
from urllib.parse import parse_qs

import httpx
from fastapi import Request

from collab_hub_api.config import GitHubConnectorConfig
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorPermissionError,
    ConnectorReconnectRequired,
    ConnectorTokenError,
)
from collab_hub_api.frames.auth import get_bearer_token

logger = logging.getLogger("frames_server.connectors")


class GitHubTokenProvider:
    def __init__(self, config: GitHubConnectorConfig):
        self.config = config

    async def access_token(self, request: Request) -> str:
        if self.config.static_access_token:
            return self.config.static_access_token

        if not self.config.broker_token_url:
            raise ConnectorNotConnected("GitHub connector token broker is not configured")

        hub_token = get_bearer_token(request)
        if not hub_token:
            raise ConnectorReconnectRequired("Hub bearer token is required for GitHub connector token brokering")

        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.config.broker_token_url,
                    headers={"Authorization": f"Bearer {hub_token}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ConnectorTokenError("GitHub token broker request failed") from exc

        logger.info("github broker token response status=%s", response.status_code)

        if response.status_code == 404:
            raise ConnectorNotConnected("GitHub account is not linked")
        if response.status_code == 403:
            raise ConnectorPermissionError(
                "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
            )
        if response.status_code in {400, 401}:
            raise ConnectorReconnectRequired("GitHub account must be reconnected")
        if response.status_code >= 400:
            raise ConnectorTokenError("GitHub token broker request failed")

        token = _extract_access_token(response)
        if not token:
            raise ConnectorReconnectRequired("GitHub token broker did not return an access token")
        return token


def _extract_access_token(response: httpx.Response) -> str:
    """Pull the access token from a Keycloak broker token response.

    Unlike the Slack/Google IdPs (which return JSON), GitHub's OAuth token
    endpoint returns **form-urlencoded** (``access_token=...&token_type=bearer``)
    and Keycloak brokers that back verbatim. Accept both shapes.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
        if isinstance(token, str) and token:
            return token
    # Fall back to form-urlencoded (GitHub's default token response shape).
    values = parse_qs(response.text or "").get("access_token")
    if values and isinstance(values[0], str) and values[0]:
        return values[0]
    return ""
