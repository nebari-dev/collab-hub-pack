from __future__ import annotations

import logging

import httpx
from fastapi import Request

from collab_hub_api.config import SlackConnectorConfig
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorPermissionError,
    ConnectorReconnectRequired,
    ConnectorTokenError,
)
from collab_hub_api.frames.auth import get_bearer_token

logger = logging.getLogger("frames_server.connectors")


class SlackTokenProvider:
    def __init__(self, config: SlackConnectorConfig):
        self.config = config

    async def access_token(self, request: Request) -> str:
        if self.config.static_access_token:
            return self.config.static_access_token

        if not self.config.broker_token_url:
            raise ConnectorNotConnected("Slack connector token broker is not configured")

        hub_token = get_bearer_token(request)
        if not hub_token:
            raise ConnectorReconnectRequired("Hub bearer token is required for Slack connector token brokering")

        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.config.broker_token_url,
                    headers={"Authorization": f"Bearer {hub_token}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ConnectorTokenError("Slack token broker request failed") from exc

        logger.info("slack broker token response status=%s", response.status_code)

        if response.status_code == 404:
            raise ConnectorNotConnected("Slack account is not linked")
        if response.status_code == 403:
            raise ConnectorPermissionError(
                "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
            )
        if response.status_code in {400, 401}:
            raise ConnectorReconnectRequired("Slack account must be reconnected")
        if response.status_code >= 400:
            raise ConnectorTokenError("Slack token broker request failed")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorTokenError("Slack token broker returned invalid JSON") from exc

        token = payload.get("access_token") or payload.get("token")
        if not isinstance(token, str) or not token:
            raise ConnectorReconnectRequired("Slack token broker did not return an access token")
        return token
