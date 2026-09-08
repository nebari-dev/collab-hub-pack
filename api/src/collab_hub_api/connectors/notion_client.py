from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .connector_text import sanitize_connector_text
from .models import NotionPageReadResponse, NotionSearchHit

# Notion pagination is cursor-based and page_size caps at 100. Bound every walk
# with a fixed page count (never ``while has_more``) so a provider cursor cycle
# is a 502, not an infinite loop.
_NOTION_MAX_PAGE_SIZE = 100
_MAX_SEARCH_PAGES = 20
_MAX_BLOCK_PAGES = 10
_MAX_BLOCK_DEPTH = 3

# Block types whose ``rich_text`` we assemble into readable page text.
_TEXT_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
}


class NotionUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Notion {operation} failed"
        if status_code is not None:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class NotionSearchError(ValueError):
    """Raised for caller-correctable Notion parameters -> 422."""


@dataclass
class NotionAccessCheck:
    workspace_name: str = ""


@dataclass
class _BlockWalkState:
    max_chars: int
    chars: int = 0
    has_more: bool = False

    def stop(self) -> bool:
        # Overshoot the budget slightly; the caller slices to max_chars and the
        # overflow sets ``truncated``. Stopping the walk here bounds provider I/O.
        return self.chars > self.max_chars


class NotionClient:
    """Read-only Notion client. Only read methods are implemented.

    Every request goes through ``_request``, which always stamps the pinned
    ``Notion-Version`` and the workspace bot token. ``POST`` is used for
    ``/v1/search`` and ``/v1/databases/{id}/query`` -- those are reads expressed
    as POST; there is still no write path.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str,
        notion_version: str,
        timeout_seconds: float = 10.0,
    ):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.notion_version = notion_version
        self.timeout = httpx.Timeout(timeout_seconds)

    async def verify_access(self) -> NotionAccessCheck:
        # Two-step capability probe (contract 4): /users/me proves the token
        # brokers; a bounded /v1/search proves it can actually read shared
        # content. A token that brokers but cannot read must not report connected.
        me = await self._request("GET", "/v1/users/me", operation="access check")
        await self._request("POST", "/v1/search", json={"page_size": 1}, operation="access check")
        return NotionAccessCheck(workspace_name=_workspace_name(me))

    async def search(
        self,
        *,
        query: str,
        object_type: str,
        limit: int,
        start_cursor: str,
        lower: datetime | None = None,
        upper: datetime | None = None,
    ) -> tuple[list[NotionSearchHit], str]:
        """One or more Notion search pages, filtered to the resolved window.

        Notion's ``/v1/search`` only SORTS by ``last_edited_time`` -- it does not
        filter by date -- so the window is applied here, Hub-side: sort newest
        first, skip items newer than ``upper`` while paging on, and stop once an
        item is older than ``lower`` (everything after it is older too). Notion
        cursors resume only at page boundaries, so ``page_size`` is aligned to
        ``limit`` and whole pages are consumed; the echoed cursor is the provider
        ``next_cursor`` of the last page actually read.
        """
        body_filter: dict | None = None
        if object_type in {"page", "database"}:
            body_filter = {"value": object_type, "property": "object"}

        results: list[NotionSearchHit] = []
        cursor = start_cursor.strip()
        page_size = min(max(limit, 1), _NOTION_MAX_PAGE_SIZE)
        for _page in range(_MAX_SEARCH_PAGES):
            body: dict = {
                "page_size": page_size,
                "sort": {"timestamp": "last_edited_time", "direction": "descending"},
            }
            if query.strip():
                body["query"] = query.strip()
            if body_filter is not None:
                body["filter"] = body_filter
            if cursor:
                body["start_cursor"] = cursor
            payload = await self._request("POST", "/v1/search", json=body, operation="search")
            next_cursor = _string(payload.get("next_cursor"))
            has_more = bool(payload.get("has_more"))
            for item in _results(payload):
                hit = _search_hit(item)
                if hit is None:
                    continue
                edited = _parse_time(hit.last_edited_time)
                if upper is not None and edited is not None and edited > upper:
                    continue  # newer than the window; keep paging
                if lower is not None and edited is not None and edited < lower:
                    # Sorted newest-first: this and everything after it is older.
                    return results, ""
                results.append(hit)
                if len(results) >= limit:
                    return results, next_cursor if has_more else ""
            if not has_more:
                return results, ""
            if next_cursor and next_cursor == cursor:
                raise NotionUpstreamError(operation="search", message="provider pagination did not advance")
            cursor = next_cursor
        # Page cap reached with the window still open; return a partial page with
        # the cursor to resume rather than looping forever.
        return results, cursor

    async def read_page(self, *, page_id: str, max_chars: int) -> NotionPageReadResponse:
        page = await self._request("GET", f"/v1/pages/{_path_segment(page_id)}", operation="page read")
        title = _page_title(page)
        parts: list[str] = []
        state = _BlockWalkState(max_chars=max_chars)
        await self._walk_blocks(page_id, parts, state, depth=0)
        assembled = sanitize_connector_text(_normalize_text("\n".join(part for part in parts if part)))
        truncated = len(assembled) > max_chars
        return NotionPageReadResponse(
            id=_string(page.get("id")) or page_id,
            title=title,
            text=assembled[:max_chars],
            truncated=truncated,
            has_more=state.has_more,
        )

    async def query_database(
        self,
        *,
        database_id: str,
        limit: int,
        start_cursor: str,
        lower: datetime | None = None,
        upper: datetime | None = None,
    ) -> tuple[list[NotionSearchHit], str]:
        """Query a database, filtering by ``last_edited_time`` NATIVELY.

        Unlike search, database query accepts a real timestamp filter, so the
        resolved window is translated to Notion's ``{"timestamp":
        "last_edited_time", ...}`` filter and sent to the provider.
        """
        body: dict = {
            "page_size": min(max(limit, 1), _NOTION_MAX_PAGE_SIZE),
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
        timestamp_filter = _timestamp_filter(lower, upper)
        if timestamp_filter is not None:
            body["filter"] = timestamp_filter
        if start_cursor.strip():
            body["start_cursor"] = start_cursor.strip()
        payload = await self._request(
            "POST",
            f"/v1/databases/{_path_segment(database_id)}/query",
            json=body,
            operation="database query",
        )
        rows: list[NotionSearchHit] = []
        for item in _results(payload):
            hit = _search_hit(item)
            if hit is not None:
                rows.append(hit)
            if len(rows) >= limit:
                break
        next_cursor = _string(payload.get("next_cursor")) if payload.get("has_more") else ""
        return rows[:limit], next_cursor

    async def _walk_blocks(self, block_id: str, parts: list[str], state: _BlockWalkState, *, depth: int) -> None:
        if state.stop():
            state.has_more = True
            return
        cursor = ""
        for _page in range(_MAX_BLOCK_PAGES):
            params = {"page_size": str(_NOTION_MAX_PAGE_SIZE)}
            if cursor:
                params["start_cursor"] = cursor
            payload = await self._request(
                "GET",
                f"/v1/blocks/{_path_segment(block_id)}/children",
                params=params,
                operation="block children",
            )
            for block in _results(payload):
                if state.stop():
                    state.has_more = True
                    return
                line = _block_text(block)
                if line:
                    parts.append(line)
                    state.chars += len(line) + 1
                if block.get("has_children"):
                    child_id = _string(block.get("id"))
                    if child_id and depth < _MAX_BLOCK_DEPTH:
                        await self._walk_blocks(child_id, parts, state, depth=depth + 1)
                    else:
                        # Nested content beyond the depth cap is left unread.
                        state.has_more = True
            if not payload.get("has_more"):
                return
            next_cursor = _string(payload.get("next_cursor"))
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor
        # More child pages remain than the page cap allows.
        state.has_more = True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict[str, str] | None = None,
        operation: str,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Notion-Version": self.notion_version,
            "Accept": "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                self.api_base_url + path,
                headers=headers,
                json=json,
                params=params,
            )
        _raise_for_notion_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message="provider returned invalid JSON",
            ) from exc
        return payload if isinstance(payload, dict) else {}


def notion_time_bounds(
    *,
    days_back: int,
    since_date: date | None,
    until_date: date | None,
    time_zone: str,
) -> tuple[datetime | None, datetime | None]:
    """Resolve friendly date fields to UTC lower/upper bounds in the given zone.

    Returns ``(None, None)`` when no filter was requested. The zone is already
    validated in the request model (a bad zone is a 422 there); this defends in
    depth anyway.
    """
    if not days_back and since_date is None and until_date is None:
        return None, None
    zone = _time_zone(time_zone)
    now = datetime.now(zone)
    lower: datetime | None
    if since_date is not None:
        lower = datetime.combine(since_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    elif days_back > 0:
        lower = (now - timedelta(days=days_back)).astimezone(timezone.utc)
    else:
        lower = None
    upper: datetime | None
    if until_date is not None:
        # Resolve the exclusive next local midnight so the whole until_date is
        # included regardless of the provider's default zone.
        upper = datetime.combine(until_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    else:
        upper = None
    return lower, upper


def _timestamp_filter(lower: datetime | None, upper: datetime | None) -> dict | None:
    inner: dict[str, str] = {}
    if lower is not None:
        inner["on_or_after"] = _rfc3339(lower)
    if upper is not None:
        inner["on_or_before"] = _rfc3339(upper)
    if not inner:
        return None
    return {"timestamp": "last_edited_time", "last_edited_time": inner}


def _search_hit(item: object) -> NotionSearchHit | None:
    if not isinstance(item, dict):
        return None
    object_type = _string(item.get("object"))
    if object_type not in {"page", "database"}:
        return None
    item_id = _string(item.get("id"))
    if not item_id:
        return None
    title = _database_title(item) if object_type == "database" else _page_title(item)
    return NotionSearchHit(
        id=item_id,
        object=object_type,  # type: ignore[arg-type]
        title=title,
        last_edited_time=_string(item.get("last_edited_time")),
    )


def _page_title(page: object) -> str:
    """Title of a page = the ``rich_text`` of its property whose type is 'title'."""
    if not isinstance(page, dict):
        return ""
    properties = page.get("properties")
    if isinstance(properties, dict):
        for value in properties.values():
            if isinstance(value, dict) and value.get("type") == "title":
                return _rich_text_plain(value.get("title"))
    return ""


def _database_title(database: object) -> str:
    if not isinstance(database, dict):
        return ""
    return _rich_text_plain(database.get("title"))


def _block_text(block: object) -> str:
    if not isinstance(block, dict):
        return ""
    block_type = _string(block.get("type"))
    if block_type not in _TEXT_BLOCK_TYPES:
        return ""
    detail = block.get(block_type)
    if not isinstance(detail, dict):
        return ""
    return _rich_text_plain(detail.get("rich_text"))


def _rich_text_plain(rich_text: object) -> str:
    """Join ``plain_text`` from a rich_text array, DROPPING every ``href``.

    Notion attaches an optional ``href`` to each rich_text element; links crash
    the Apollo chat renderer (apollo-desktop#365), so only ``plain_text`` is kept.
    """
    if not isinstance(rich_text, list):
        return ""
    parts = [
        element["plain_text"]
        for element in rich_text
        if isinstance(element, dict) and isinstance(element.get("plain_text"), str)
    ]
    return "".join(parts).strip()


def _workspace_name(me: object) -> str:
    if isinstance(me, dict):
        bot = me.get("bot")
        if isinstance(bot, dict):
            return sanitize_connector_text(_string(bot.get("workspace_name")))
    return ""


def _results(payload: dict) -> list:
    results = payload.get("results")
    return results if isinstance(results, list) else []


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _time_zone(value: str) -> ZoneInfo:
    name = value.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise NotionSearchError(f"Unknown IANA time_zone {name!r}.") from exc


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _raise_for_notion_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise NotionUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_notion_error_message(response),
        ) from exc


def _notion_error_message(response: httpx.Response) -> str:
    """Notion errors are ``{"object":"error","status":...,"code":...,"message":...}``.

    Only the structured ``message`` is surfaced; a non-JSON body (e.g. an
    intermediary proxy's HTML error page) is never echoed to the caller.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message[:240].strip()
    return ""
