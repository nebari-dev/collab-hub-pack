from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .models import SlackChannel, SlackMessage, SlackSearchHit
from .slack_text import sanitize_slack_text

DM_TYPES = ["im", "mpim"]

MAX_LIST_PAGE_SIZE = 200
MAX_LIST_PAGES = 5
MAX_CHANNEL_AUTHORIZATION_PAGES = 25

# ``auth.test`` errors that mean the brokered token is not a usable Slack Web API
# user token -- e.g. Keycloak brokered an OpenID sign-in/identity token instead of an
# ``xoxp`` user token. These map the connector status to "reconnect required" so a
# linked-but-unusable token surfaces before every read fails.
SLACK_TOKEN_INVALID_ERRORS = frozenset(
    {
        "not_authed",
        "no_authed",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_allowed_token_type",
    }
)


@dataclass
class SlackAccessCheck:
    """Result of validating a token against Slack's ``auth.test`` read endpoint."""

    user_id: str = ""
    team: str = ""
    granted_scopes: list[str] = field(default_factory=list)


class SlackUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Slack {operation} failed"
        if status_code is not None and status_code != 200:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class SlackConversationNotAllowed(RuntimeError):
    """Raised when a raw conversation id is not an ordinary Slack channel."""


class SlackClient:
    """Read-only Slack Web API client. Only GET-style read methods are implemented."""

    def __init__(self, *, access_token: str, api_base_url: str, timeout_seconds: float = 10.0):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def list_channels(
        self,
        *,
        limit: int,
        cursor: str = "",
        max_pages: int = MAX_LIST_PAGES,
    ) -> tuple[list[SlackChannel], str]:
        phase, upstream_cursor = _decode_channel_cursor(cursor)

        if phase == "public":
            public, next_cursor = await self._list_conversations(
                types=["public_channel"],
                limit=limit,
                cursor=upstream_cursor,
                operation="public channel list",
                max_pages=max_pages,
            )
            if next_cursor:
                return public, _encode_channel_cursor("public", next_cursor)
            return public, _encode_channel_cursor("private", "")

        private, next_cursor = await self._list_conversations(
            types=["private_channel"],
            limit=limit,
            cursor=upstream_cursor,
            operation="private channel list",
            max_pages=max_pages,
        )
        if next_cursor:
            return private, _encode_channel_cursor("private", next_cursor)
        return private, ""

    async def list_dms(self, *, limit: int, cursor: str = "") -> tuple[list[SlackChannel], str]:
        return await self._list_conversations(
            types=DM_TYPES,
            limit=limit,
            cursor=cursor,
            operation="dm list",
        )

    async def search(self, *, query: str, limit: int) -> list[SlackSearchHit]:
        hits, _ = await self.search_page(query=query, limit=limit, page=1)
        return hits

    async def search_page(self, *, query: str, limit: int, page: int = 1) -> tuple[list[SlackSearchHit], int | None]:
        payload = await self._get_json(
            "/search.messages",
            params={
                "query": query,
                "count": str(limit),
                "page": str(page),
                "sort": "timestamp",
                "sort_dir": "desc",
                "highlight": "false",
            },
            operation="search",
        )
        matches = payload.get("messages", {}).get("matches", [])
        if not isinstance(matches, list):
            return [], None
        paging = payload.get("messages", {}).get("paging", {})
        pages = int(paging.get("pages", page) or page) if isinstance(paging, dict) else page
        next_page = page + 1 if page < pages else None
        hits = [_search_hit(item) for item in matches[:limit]]
        return [hit for hit in hits if not (hit.is_im or hit.is_mpim)], next_page

    async def read_conversation(
        self,
        *,
        channel_id: str,
        limit: int,
        oldest: str = "",
        latest: str = "",
        cursor: str = "",
    ) -> tuple[list[SlackMessage], bool, str]:
        await self._require_channel(channel_id)
        params = {
            "channel": channel_id,
            "limit": str(limit),
            "inclusive": "true",
        }
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        if cursor:
            params["cursor"] = cursor
        payload = await self._get_json("/conversations.history", params=params, operation="conversation read")
        return _messages_page(payload, channel_id)

    async def read_thread(
        self,
        *,
        channel_id: str,
        message_ts: str,
        limit: int,
        cursor: str = "",
    ) -> tuple[list[SlackMessage], bool, str]:
        await self._require_channel(channel_id)
        params = {
            "channel": channel_id,
            "ts": message_ts,
            "limit": str(limit),
            "inclusive": "true",
        }
        if cursor:
            params["cursor"] = cursor
        payload = await self._get_json("/conversations.replies", params=params, operation="thread read")
        return _messages_page(payload, channel_id)

    async def _require_channel(self, channel_id: str) -> None:
        try:
            payload = await self._get_json(
                "/conversations.info",
                params={"channel": channel_id},
                operation="conversation metadata",
            )
        except SlackUpstreamError as exc:
            if exc.message != "missing_scope":
                raise
            cursor = ""
            seen: set[str] = set()
            for _page in range(MAX_CHANNEL_AUTHORIZATION_PAGES):
                channels, next_cursor = await self.list_channels(
                    limit=MAX_LIST_PAGE_SIZE,
                    cursor=cursor,
                    max_pages=1,
                )
                if any(channel.id == channel_id for channel in channels):
                    return
                if not next_cursor or next_cursor in seen:
                    break
                seen.add(next_cursor)
                cursor = next_cursor
            raise SlackConversationNotAllowed("Only Slack channels are allowed") from exc
        item = payload.get("channel")
        if not isinstance(item, dict):
            raise SlackConversationNotAllowed("Only Slack channels are allowed")
        if item.get("is_im") or item.get("is_mpim"):
            raise SlackConversationNotAllowed("Direct-message conversations are not allowed")
        if not (item.get("is_channel") or item.get("is_group")):
            raise SlackConversationNotAllowed("Only Slack channels are allowed")

    async def verify_access(self) -> SlackAccessCheck:
        """Confirm the token is a usable Slack Web API user token via ``auth.test``.

        Raises :class:`SlackUpstreamError` when Slack rejects the token; an OpenID
        sign-in/identity token surfaces as ``invalid_auth`` / ``not_authed`` (see
        :data:`SLACK_TOKEN_INVALID_ERRORS`). On success the granted user scopes are
        read from the ``x-oauth-scopes`` response header when Slack provides it.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + "/auth.test",
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            )
        _raise_for_slack_status(response, operation="auth check")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SlackUpstreamError(
                operation="auth check", status_code=response.status_code, message="invalid response"
            )
        if not payload.get("ok", False):
            raise SlackUpstreamError(
                operation="auth check",
                status_code=response.status_code,
                message=str(payload.get("error", "unknown_error")),
            )
        granted = response.headers.get("x-oauth-scopes", "")
        scopes = [scope.strip() for scope in granted.split(",") if scope.strip()]
        return SlackAccessCheck(
            user_id=str(payload.get("user_id", "") or ""),
            team=str(payload.get("team", "") or ""),
            granted_scopes=scopes,
        )

    async def _list_conversations(
        self,
        *,
        types: list[str],
        limit: int,
        cursor: str,
        operation: str,
        max_pages: int = MAX_LIST_PAGES,
    ) -> tuple[list[SlackChannel], str]:
        channels: list[SlackChannel] = []
        next_cursor = cursor
        for _page in range(max_pages):
            params = {
                "types": ",".join(types),
                "exclude_archived": "true",
                "limit": str(min(max(limit, 1), MAX_LIST_PAGE_SIZE)),
            }
            if next_cursor:
                params["cursor"] = next_cursor
            payload = await self._get_json("/conversations.list", params=params, operation=operation)
            items = payload.get("channels", [])
            if isinstance(items, list):
                channels.extend(_channel(item) for item in items)
            next_cursor = payload.get("response_metadata", {}).get("next_cursor", "") or ""
            if not next_cursor or len(channels) >= limit:
                break
        return channels[:limit], next_cursor

    async def _get_json(self, path: str, params: dict[str, str], *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                params=params,
            )
        _raise_for_slack_status(response, operation=operation)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SlackUpstreamError(operation=operation, status_code=response.status_code, message="invalid response")
        if not payload.get("ok", False):
            raise SlackUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message=str(payload.get("error", "unknown_error")),
            )
        return payload


def _messages_page(payload: dict, channel_id: str) -> tuple[list[SlackMessage], bool, str]:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    next_cursor = payload.get("response_metadata", {}).get("next_cursor", "") or ""
    return (
        [_message(item, channel_id) for item in messages],
        bool(payload.get("has_more", False)),
        next_cursor,
    )


def _encode_channel_cursor(phase: str, upstream_cursor: str) -> str:
    return f"{phase}:{upstream_cursor}"


def _decode_channel_cursor(cursor: str) -> tuple[str, str]:
    phase, separator, upstream_cursor = cursor.partition(":")
    if separator and phase in {"public", "private"}:
        return phase, upstream_cursor
    return "public", cursor


def _channel(item: dict) -> SlackChannel:
    return SlackChannel(
        id=str(item.get("id", "")),
        name=str(item.get("name", "") or _dm_name(item)),
        is_private=bool(item.get("is_private", False)),
        is_im=bool(item.get("is_im", False)),
        is_mpim=bool(item.get("is_mpim", False)),
        user_id=str(item.get("user", "") or ""),
        topic=sanitize_slack_text(str((item.get("topic") or {}).get("value", "") or "")),
        num_members=item.get("num_members"),
    )


def _dm_name(item: dict) -> str:
    if item.get("is_im"):
        user = item.get("user", "")
        return f"dm-{user}" if user else "dm"
    return ""


def _message(item: dict, channel_id: str) -> SlackMessage:
    return SlackMessage(
        channel_id=channel_id,
        ts=str(item.get("ts", "")),
        user_id=str(item.get("user") or item.get("bot_id") or ""),
        text=sanitize_slack_text(str(item.get("text", "") or "")),
        thread_ts=str(item.get("thread_ts", "") or ""),
        reply_count=int(item.get("reply_count", 0) or 0),
    )


def _search_hit(item: dict) -> SlackSearchHit:
    channel = item.get("channel") or {}
    return SlackSearchHit(
        channel_id=str(channel.get("id", "") or ""),
        channel_name=str(channel.get("name", "") or ""),
        is_im=bool(channel.get("is_im", False)),
        is_mpim=bool(channel.get("is_mpim", False)),
        ts=str(item.get("ts", "")),
        user_id=str(item.get("user", "") or ""),
        author_name=str(item.get("username", "") or ""),
        text=sanitize_slack_text(str(item.get("text", "") or "")),
    )


def _raise_for_slack_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SlackUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_slack_error_message(response),
        ) from exc


def _slack_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:240].strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error[:240].strip()
    return ""
