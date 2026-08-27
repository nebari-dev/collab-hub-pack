from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

import httpx

from .models import DriveFileMetadata, GoogleDriveFile

GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

DIRECT_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "text/csv",
    "text/markdown",
    "text/plain",
}

SEARCH_STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "any",
    "are",
    "but",
    "can",
    "did",
    "does",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "not",
    "our",
    "that",
    "the",
    "this",
    "with",
    "what",
    "when",
    "where",
    "who",
    "why",
}


class UnsupportedDriveFileType(RuntimeError):
    pass


class DriveUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Google Drive {operation} failed"
        if status_code is not None:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class GoogleDriveClient:
    def __init__(self, *, access_token: str, api_base_url: str, timeout_seconds: float = 10.0):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def search(
        self,
        *,
        query: str,
        limit: int,
        modified_after: datetime | None = None,
        mime_types: list[str] | None = None,
    ) -> list[DriveFileMetadata]:
        search_terms = _search_terms(query)
        base_params = {
            "pageSize": str(_search_page_size(limit)),
            "fields": "files(id,name,mimeType,modifiedTime,owners(displayName,emailAddress))",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        name_queries = [
            _name_query(phrase, modified_after=modified_after, mime_types=mime_types or [])
            for phrase in _name_search_phrases(query)
        ]
        fallback_queries = [
            _drive_query(
                query,
                modified_after=modified_after,
                mime_types=mime_types or [],
                terms=search_terms[:8],
                include_full_text=False,
            ),
            _drive_query(
                query,
                modified_after=modified_after,
                mime_types=mime_types or [],
                terms=search_terms[:8],
                include_full_text=True,
            ),
        ]

        payloads: list[dict] = []
        last_error: DriveUpstreamError | None = None
        for drive_query in _unique(name_queries):
            params = dict(base_params)
            params["q"] = drive_query
            try:
                payload = await self._get_json("/files", params=params, operation="search")
                payloads.append(payload)
                if _payload_file_count(payload) >= limit:
                    break
            except DriveUpstreamError as exc:
                last_error = exc

        if not _payloads_have_files(payloads):
            for drive_query in _unique(fallback_queries):
                params = dict(base_params)
                params["q"] = drive_query
                try:
                    payloads.append(await self._get_json("/files", params=params, operation="search"))
                except DriveUpstreamError as exc:
                    last_error = exc

        if not payloads and last_error is not None:
            raise last_error

        by_id: dict[str, DriveFileMetadata] = {}
        for payload in payloads:
            files = payload.get("files", [])
            if not isinstance(files, list):
                continue
            for item in files:
                metadata = GoogleDriveFile.model_validate(item).to_metadata()
                by_id.setdefault(metadata.id, metadata)
        results = list(by_id.values())
        results.sort(key=lambda item: _search_score(item, query, search_terms), reverse=True)
        return results[:limit]

    async def metadata(self, file_id: str) -> DriveFileMetadata:
        payload = await self._get_json(
            f"/files/{_path_segment(file_id)}",
            params={
                "fields": "id,name,mimeType,modifiedTime,owners(displayName,emailAddress)",
                "supportsAllDrives": "true",
            },
            operation="metadata",
        )
        return GoogleDriveFile.model_validate(payload).to_metadata()

    async def read_text(self, file: DriveFileMetadata, max_chars: int) -> tuple[str, bool]:
        max_bytes = (max_chars * 4) + 1
        file_id = _path_segment(file.id)
        if file.mime_type in GOOGLE_DOC_EXPORTS:
            body, byte_truncated = await self._get_limited_bytes(
                f"/files/{file_id}/export",
                params={"mimeType": GOOGLE_DOC_EXPORTS[file.mime_type]},
                max_bytes=max_bytes,
                operation="export",
            )
        elif file.mime_type in DIRECT_TEXT_MIME_TYPES or file.mime_type.startswith("text/"):
            body, byte_truncated = await self._get_limited_bytes(
                f"/files/{file_id}",
                params={"alt": "media", "supportsAllDrives": "true"},
                max_bytes=max_bytes,
                operation="download",
            )
        else:
            raise UnsupportedDriveFileType(f"Unsupported Google Drive MIME type: {file.mime_type}")

        text = body.decode("utf-8", errors="replace")
        text = _normalize_text(text)
        truncated = byte_truncated or len(text) > max_chars
        return text[:max_chars], truncated

    async def _get_json(self, path: str, params: dict[str, str], *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                params=params,
            )
        _raise_for_drive_status(response, operation=operation)
        return response.json()

    async def _get_limited_bytes(
        self,
        path: str,
        params: dict[str, str],
        *,
        max_bytes: int,
        operation: str,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "GET",
                self.api_base_url + path,
                headers={"Authorization": f"Bearer {self.access_token}"},
                params=params,
            ) as response:
                _raise_for_drive_status(response, operation=operation)
                async for chunk in response.aiter_bytes():
                    remaining = (max_bytes + 1) - total
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        truncated = True
                        break
        return b"".join(chunks)[:max_bytes], truncated


def _drive_query(
    query: str,
    *,
    modified_after: datetime | None,
    mime_types: list[str],
    include_full_text: bool,
    terms: list[str] | None = None,
) -> str:
    text_clauses = []
    search_terms = terms if terms is not None else _search_terms(query)
    for term in search_terms:
        escaped_term = _quote(term)
        if include_full_text:
            text_clauses.append(f"fullText contains '{escaped_term}'")
        text_clauses.append(f"name contains '{escaped_term}'")
    if not text_clauses:
        text_clauses.append("name contains ''")
    clauses = ["trashed = false", f"({' or '.join(text_clauses)})"]
    if modified_after:
        clauses.append(f"modifiedTime > '{modified_after.isoformat()}'")
    clean_mime_types = [_quote(value.strip()) for value in mime_types if value.strip()]
    if clean_mime_types:
        mime_clause = " or ".join(f"mimeType = '{value}'" for value in clean_mime_types)
        clauses.append(f"({mime_clause})")
    return " and ".join(clauses)


def _name_query(phrase: str, *, modified_after: datetime | None, mime_types: list[str]) -> str:
    clauses = ["trashed = false", f"name contains '{_quote(phrase)}'"]
    if modified_after:
        clauses.append(f"modifiedTime > '{modified_after.isoformat()}'")
    clean_mime_types = [_quote(value.strip()) for value in mime_types if value.strip()]
    if clean_mime_types:
        mime_clause = " or ".join(f"mimeType = '{value}'" for value in clean_mime_types)
        clauses.append(f"({mime_clause})")
    return " and ".join(clauses)


def _search_page_size(limit: int) -> int:
    return min(100, max(limit, 25) * 4)


def _payloads_have_files(payloads: list[dict]) -> bool:
    return any(_payload_file_count(payload) > 0 for payload in payloads)


def _payload_file_count(payload: dict) -> int:
    files = payload.get("files", [])
    return len(files) if isinstance(files, list) else 0


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _normalize_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _search_terms(query: str) -> list[str]:
    query = query.strip()
    if not query:
        return []

    terms: list[str] = [query]
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query)
        if len(word) >= 3 and word.lower() not in SEARCH_STOP_WORDS
    ]
    terms.extend(words)
    if len(words) > 2:
        terms.extend(" ".join(triple) for triple in zip(words, words[1:], words[2:], strict=False))
        terms.extend(" ".join(pair) for pair in zip(words, words[1:], strict=False))

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        normalized = " ".join(term.split())
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique_terms.append(normalized)
        if len(unique_terms) >= 24:
            break
    return unique_terms


def _name_search_phrases(query: str) -> list[str]:
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query)
        if len(word) >= 3 and word.lower() not in SEARCH_STOP_WORDS
    ]
    phrases = [" ".join(words)] if words else []
    for size in (3, 4, 5):
        if len(words) >= size:
            phrases.extend(" ".join(words[index : index + size]) for index in range(0, len(words) - size + 1))
    return _unique(phrases)[:8]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique_values.append(normalized)
    return unique_values


def _search_score(file: DriveFileMetadata, query: str, terms: list[str]) -> int:
    name = file.name.lower()
    normalized_query = " ".join(query.lower().split())
    score = 0
    if normalized_query and normalized_query in name:
        score += 100
    for term in terms:
        normalized = term.lower()
        if not normalized:
            continue
        if normalized == name:
            score += 80
        elif normalized in name:
            score += 20 + len(normalized)
    return score


def _raise_for_drive_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise DriveUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_drive_error_message(response),
        ) from exc


def _drive_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:240].strip()
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message[:240].strip()
    return ""
