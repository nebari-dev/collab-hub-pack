from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel


class UserDirectoryUnavailableError(RuntimeError):
    """Raised when the configured user directory cannot serve a request."""


class UserDirectoryUser(BaseModel):
    id: str
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    enabled: bool = True


class UserDirectoryGroup(BaseModel):
    id: str
    name: str
    path: str | None = None


class UserDirectoryClient(Protocol):
    def search_users(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryUser]: ...

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]: ...

    def close(self) -> None: ...


class DisabledUserDirectoryClient:
    def search_users(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryUser]:
        raise UserDirectoryUnavailableError("User directory is not configured")

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]:
        raise UserDirectoryUnavailableError("User directory is not configured")

    def close(self) -> None:
        return None


@dataclass
class _AccessToken:
    value: str
    expires_at: float


class KeycloakUserDirectoryClient:
    def __init__(
        self,
        *,
        token_url: str,
        admin_api_base_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_url = token_url.rstrip("/")
        self.admin_api_base_url = admin_api_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._token: _AccessToken | None = None

    def search_users(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryUser]:
        params: dict[str, str | int | bool] = {"max": limit}
        if query:
            params["search"] = query
        payload = self._admin_get("/users", params=params)
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak users response was not a list")
        return [_user_from_keycloak(item) for item in payload if isinstance(item, dict)]

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]:
        params: dict[str, str | int | bool] = {"max": limit, "briefRepresentation": True}
        if query:
            params["search"] = query
        payload = self._admin_get("/groups", params=params)
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak groups response was not a list")
        return [_group_from_keycloak(item) for item in payload if isinstance(item, dict)]

    def close(self) -> None:
        self._client.close()

    def _admin_get(self, path: str, *, params: dict[str, str | int | bool]) -> object:
        try:
            response = self._admin_get_once(path, params=params)
            if response.status_code == 401:
                self._token = None
                response = self._admin_get_once(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise UserDirectoryUnavailableError(
                f"Keycloak user directory request failed with HTTP {status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise UserDirectoryUnavailableError("Keycloak user directory request failed") from exc

    def _admin_get_once(self, path: str, *, params: dict[str, str | int | bool]) -> httpx.Response:
        return self._client.get(
            f"{self.admin_api_base_url}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params=params,
        )

    def _access_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 30:
            return self._token.value

        try:
            response = self._client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UserDirectoryUnavailableError("Keycloak token request failed") from exc

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UserDirectoryUnavailableError("Keycloak token response did not include an access token")
        expires_in = payload.get("expires_in")
        ttl = expires_in if isinstance(expires_in, int | float) else 60
        self._token = _AccessToken(value=access_token, expires_at=now + max(float(ttl), 0.0))
        return access_token


def _user_from_keycloak(payload: dict) -> UserDirectoryUser:
    return UserDirectoryUser(
        id=str(payload.get("id") or ""),
        username=str(payload.get("username") or ""),
        email=_optional_str(payload.get("email")),
        first_name=_optional_str(payload.get("firstName")),
        last_name=_optional_str(payload.get("lastName")),
        enabled=bool(payload.get("enabled", True)),
    )


def _group_from_keycloak(payload: dict) -> UserDirectoryGroup:
    return UserDirectoryGroup(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        path=_optional_str(payload.get("path")),
    )


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
