from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .connector_text import sanitize_connector_text
from .models import (
    GitHubItem,
    GitHubProject,
    GitHubProjectItem,
    GitHubRepo,
    GitHubReview,
    GitHubSearchHit,
)

# GitHub search returns at most 1000 results across all pages, regardless of
# total_count, and caps per_page at 100. We never page past the 1000th result.
GITHUB_SEARCH_RESULT_CAP = 1000
GITHUB_MAX_PER_PAGE = 100
# Cap the body text carried in a search hit; a full issue/PR body belongs to a
# dedicated read, not a search snippet.
SEARCH_TEXT_SNIPPET_CHARS = 500
# The contents API only returns files up to 1 MB; larger ones come back with
# encoding "none" and no content, and must be fetched via the git blobs API.
CONTENTS_MAX_BYTES = 1_000_000
# Bound how many comments a single item read pulls, independent of max_chars.
MAX_ITEM_COMMENTS = 30
# PR reviews come back oldest-first with no sort option; fetch a wide page and
# keep the newest tail so a later APPROVED isn't hidden by an early CHANGES_REQUESTED.
_PR_REVIEW_PAGE_SIZE = 100
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec"

# Projects V2 are GraphQL-only. Query the org and user owners in one request and
# use whichever resolves, so one tool handles org and personal boards.
_PROJECT_ITEM_FIELDS = """
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          type
          content {
            __typename
            ... on Issue { number title repository { nameWithOwner } }
            ... on PullRequest { number title repository { nameWithOwner } }
            ... on DraftIssue { title }
          }
          fieldValues(first: 8) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
"""

_PROJECT_SUMMARY = "number title shortDescription closed items { totalCount }"

_PROJECTS_LIST_QUERY = f"""
query($login: String!, $first: Int!) {{
  organization(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
  user(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
}}
"""

_PROJECT_READ_BODY = f"""projectV2(number: $number) {{
      number title shortDescription closed
      items(first: $first, after: $after) {{ {_PROJECT_ITEM_FIELDS} }}
    }}"""

_PROJECT_READ_QUERY = f"""
query($login: String!, $number: Int!, $first: Int!, $after: String) {{
  organization(login: $login) {{ {_PROJECT_READ_BODY} }}
  user(login: $login) {{ {_PROJECT_READ_BODY} }}
}}
"""

# Project reads auto-paginate server-side: GitHub's GraphQL caps a page at 100,
# so we walk up to MAX_PROJECT_PAGES pages (a bounded whole-board read) and set
# truncated=True only for boards larger than that ceiling.
_PROJECT_PAGE_SIZE = 100
_MAX_PROJECT_ITEMS = 500
_MAX_PROJECT_PAGES = _MAX_PROJECT_ITEMS // _PROJECT_PAGE_SIZE


@dataclass
class GitHubFileContent:
    content: str = ""
    truncated: bool = False
    too_large: bool = False
    binary: bool = False
    unsupported_reason: str = ""


@dataclass
class GitHubAccessCheck:
    """Result of validating a token against GitHub's read endpoints."""

    login: str = ""
    scopes: list[str] = field(default_factory=list)


class GitHubUpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        status_code: int | None = None,
        message: str = "",
        retry_after: str = "",
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        detail = f"GitHub {operation} failed"
        if status_code is not None and status_code != 200:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class GitHubSearchError(ValueError):
    """Caller-correctable search parameters (e.g. a stale page_token) -> 422."""


class GitHubClient:
    """Read-only GitHub API client. Only read methods are implemented.

    Read-only is a code-level guarantee, not a token-level one: the brokered
    ``repo`` scope is write-capable (GitHub has no read-only private-repo scope),
    so this class deliberately exposes no method that issues a non-GET request.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str,
        timeout_seconds: float = 10.0,
        allowed_orgs: list[str] | None = None,
    ):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.allowed_orgs = allowed_orgs or []

    async def verify_access(self) -> GitHubAccessCheck:
        """Confirm the token can read, and resolve the connected login.

        ``/user`` validates the token and yields the login; ``/user/repos`` is
        the capability probe that fails meaningfully (401/403) when the token
        cannot enumerate repositories (bad token, or org SSO not authorized).
        """
        response = await self._get_response("/user", operation="auth check")
        await self._get("/user/repos", params={"per_page": "1"}, operation="repo access check")
        try:
            user = response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation="auth check", status_code=response.status_code, message="invalid JSON"
            ) from exc
        login = str(user.get("login", "") or "") if isinstance(user, dict) else ""
        # Report the token's real grant (X-OAuth-Scopes header), not the configured
        # intention: a stale link silently missing e.g. read:project must show as such.
        scopes = _parse_oauth_scopes(response.headers.get("x-oauth-scopes", ""))
        return GitHubAccessCheck(login=login, scopes=scopes)

    async def search(
        self,
        *,
        query: str,
        limit: int,
        repo: str = "",
        page_token: str = "",
    ) -> tuple[list[GitHubSearchHit], str, bool]:
        fingerprint = _fingerprint(query=query, repo=repo, limit=limit)
        page = _decode_page_token(page_token, fingerprint)
        per_page = min(max(limit, 1), GITHUB_MAX_PER_PAGE)

        try:
            payload = await self._get(
                "/search/issues",
                params={
                    "q": self._build_query(query, repo),
                    "per_page": str(per_page),
                    "page": str(page),
                    "sort": "updated",
                    "order": "desc",
                },
                operation="search",
            )
        except GitHubUpstreamError as exc:
            # GitHub answers 422 when a qualifier is invalid — most often a
            # repo:/org: that doesn't exist or isn't visible. Surface it as a
            # caller-fixable error so the model corrects the repo, not a 502.
            if exc.status_code == 422:
                raise GitHubSearchError(
                    "GitHub rejected the search. Check the query and that any repo/org "
                    "exists and is accessible (use owner/name for repo)."
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise GitHubUpstreamError(operation="search", message="invalid response")
        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        total = int(payload.get("total_count", 0) or 0)
        incomplete = bool(payload.get("incomplete_results", False))
        hits = [_search_hit(item) for item in items if isinstance(item, dict)]

        next_token = ""
        reachable = min(total, GITHUB_SEARCH_RESULT_CAP)
        if len(items) >= per_page and page * per_page < reachable:
            next_token = _encode_page_token(page + 1, fingerprint)
        return hits, next_token, incomplete

    async def read_item(
        self,
        *,
        repo: str,
        number: int,
        max_chars: int,
    ) -> tuple[GitHubItem, str, bool]:
        owner_repo = repo.strip()
        issue = await self._get(f"/repos/{owner_repo}/issues/{number}", operation="item read")
        if not isinstance(issue, dict):
            raise GitHubUpstreamError(operation="item read", message="invalid response")
        user = issue.get("user") or {}
        item = GitHubItem(
            repo=owner_repo,
            number=int(issue.get("number", number) or number),
            title=sanitize_connector_text(str(issue.get("title", "") or "")),
            state=str(issue.get("state", "") or ""),
            is_pull_request="pull_request" in issue,
            author=str(user.get("login", "") or "") if isinstance(user, dict) else "",
            assignees=_logins(issue.get("assignees")),
            labels=_label_names(issue.get("labels")),
            comments=int(issue.get("comments", 0) or 0),
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
        )

        # The issues endpoint returns a PR's body and comments but not its
        # requested reviewers or review states; fetch those from the pulls API.
        if item.is_pull_request:
            pull = await self._get(f"/repos/{owner_repo}/pulls/{number}", operation="pull read")
            if isinstance(pull, dict):
                item.requested_reviewers = _logins(pull.get("requested_reviewers"))
            pull_reviews = await self._get(
                f"/repos/{owner_repo}/pulls/{number}/reviews",
                params={"per_page": str(_PR_REVIEW_PAGE_SIZE)},
                operation="pull reviews",
            )
            # Keep the newest tail: reviews are oldest-first, so a later APPROVED
            # must not be dropped in favor of an earlier CHANGES_REQUESTED.
            item.reviews = _reviews(pull_reviews)[-MAX_ITEM_COMMENTS:]

        segments: list[str] = []
        body = sanitize_connector_text(str(issue.get("body", "") or ""))
        if body:
            segments.append(body)
        if item.comments:
            # Comments come back oldest-first; the count is in the issue payload,
            # so jump to the last page to return the most recent comments, not the
            # oldest 30 (which is what "recent comments" is supposed to mean).
            last_page = (item.comments + MAX_ITEM_COMMENTS - 1) // MAX_ITEM_COMMENTS
            comments = await self._get(
                f"/repos/{owner_repo}/issues/{number}/comments",
                params={"per_page": str(MAX_ITEM_COMMENTS), "page": str(last_page)},
                operation="item comments",
            )
            if isinstance(comments, list):
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    commenter = comment.get("user") or {}
                    login = str(commenter.get("login", "") or "") if isinstance(commenter, dict) else ""
                    text = sanitize_connector_text(str(comment.get("body", "") or ""))
                    segments.append(f"@{login}: {text}" if login else text)

        full = "\n\n".join(segment for segment in segments if segment)
        truncated = len(full) > max_chars
        if truncated:
            full = full[:max_chars].rstrip() + "…"
        return item, full, truncated

    async def read_file(
        self,
        *,
        repo: str,
        path: str,
        ref: str,
        max_chars: int,
    ) -> GitHubFileContent:
        owner_repo = repo.strip()
        params = {"ref": ref.strip()} if ref.strip() else None
        data = await self._get(
            f"/repos/{owner_repo}/contents/{quote(path.strip(), safe='/')}",
            params=params,
            operation="file read",
        )
        if isinstance(data, list):
            return GitHubFileContent(unsupported_reason="path is a directory, not a file")
        if not isinstance(data, dict):
            raise GitHubUpstreamError(operation="file read", message="invalid response")
        if str(data.get("type", "")) != "file":
            return GitHubFileContent(unsupported_reason=f"path is a {data.get('type', 'non-file')}, not a file")

        size = int(data.get("size", 0) or 0)
        encoding = str(data.get("encoding", "") or "")
        raw_content = data.get("content", "") or ""
        # The contents API omits content (encoding "none") for files over 1 MB.
        if size > CONTENTS_MAX_BYTES or encoding == "none" or (encoding != "base64" and not raw_content):
            return GitHubFileContent(too_large=True)

        try:
            decoded = base64.b64decode(raw_content)
        except (ValueError, TypeError):
            return GitHubFileContent(binary=True)
        if decoded.startswith(_LFS_POINTER_PREFIX):
            return GitHubFileContent(unsupported_reason="file is a git-lfs pointer, not stored inline")
        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            return GitHubFileContent(binary=True)

        text = sanitize_connector_text(text)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars].rstrip() + "…"
        return GitHubFileContent(content=text, truncated=truncated)

    async def list_projects(self, *, owner: str, first: int = 20) -> list[GitHubProject]:
        data = await self._graphql(
            _PROJECTS_LIST_QUERY, {"login": owner.strip(), "first": first}, operation="project list"
        )
        node = _owner_node(data)
        if node is None:
            raise GitHubUpstreamError(operation="project list", status_code=404, message="owner not found")
        nodes = ((node.get("projectsV2") or {}).get("nodes")) or []
        return [_project(p) for p in nodes if isinstance(p, dict)]

    async def read_project(
        self,
        *,
        owner: str,
        number: int,
        max_items: int,
    ) -> tuple[GitHubProject, list[GitHubProjectItem]]:
        """Read a board's items, auto-paginating server-side up to a bounded cap
        so the caller gets the whole board in one call (GitHub caps a GraphQL
        page at 100). project.items_count is the board's real size; the router
        marks the response truncated when we returned fewer (board > cap)."""
        cap = min(max(max_items, 1), _MAX_PROJECT_ITEMS)
        login = owner.strip()
        project: dict | None = None
        collected: list[GitHubProjectItem] = []
        cursor: str | None = None
        for _page in range(_MAX_PROJECT_PAGES):
            data = await self._graphql(
                _PROJECT_READ_QUERY,
                {"login": login, "number": number, "first": _PROJECT_PAGE_SIZE, "after": cursor},
                operation="project read",
            )
            node = _owner_node(data)
            project = node.get("projectV2") if node else None
            if not isinstance(project, dict):
                raise GitHubUpstreamError(operation="project read", status_code=404, message="project not found")
            items_field = project.get("items") or {}
            for item in (items_field.get("nodes") or []):
                if isinstance(item, dict):
                    collected.append(_project_item(item))
            if len(collected) >= cap:
                break
            page_info = items_field.get("pageInfo") or {}
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = str(page_info["endCursor"])
            else:
                break
        return _project(project or {}), collected[:cap]

    async def list_repos(self, *, owner: str, first: int = 30) -> list[GitHubRepo]:
        """List repos for an org or user (whichever the owner is), most recently
        updated first — so an agent can resolve a name to an actual repo instead
        of guessing owner/name."""
        owner = owner.strip()
        params = {"per_page": str(first), "sort": "updated", "direction": "desc"}
        try:
            data = await self._get(f"/orgs/{owner}/repos", params=params, operation="repo list")
        except GitHubUpstreamError as exc:
            if exc.status_code == 404:  # not an org (or not visible as one) — try the user
                data = await self._get(f"/users/{owner}/repos", params=params, operation="repo list")
            else:
                raise
        if not isinstance(data, list):
            return []
        return [_repo(node) for node in data if isinstance(node, dict)]

    async def _graphql(self, query: str, variables: dict, *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.api_base_url + "/graphql",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"query": query, "variables": variables},
            )
        _raise_for_github_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation=operation, status_code=response.status_code, message="invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubUpstreamError(operation=operation, message="invalid response")
        data = payload.get("data")
        # GraphQL returns HTTP 200 with a top-level errors[] for auth-scope and
        # other failures; surface only the structured message, never a raw body.
        if data is None:
            message = ""
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message", "") or "")[:240]
            raise GitHubUpstreamError(operation=operation, message=message or "GraphQL query failed")
        return data if isinstance(data, dict) else {}

    def _build_query(self, query: str, repo: str) -> str:
        parts = [query.strip()]
        if repo.strip():
            parts.append(f"repo:{repo.strip()}")
        elif self.allowed_orgs:
            # Repeating ``org:`` qualifiers ORs them in GitHub issue search, so
            # results stay confined to the configured org allowlist.
            parts.extend(f"org:{org}" for org in self.allowed_orgs)
        return " ".join(part for part in parts if part)

    async def _get_response(
        self, path: str, *, operation: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params=params,
            )
        _raise_for_github_status(response, operation=operation)
        return response

    async def _get(self, path: str, *, operation: str, params: dict[str, str] | None = None) -> Any:
        response = await self._get_response(path, operation=operation, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation=operation, status_code=response.status_code, message="invalid JSON"
            ) from exc


def _parse_oauth_scopes(header: str) -> list[str]:
    # GitHub returns the token's granted scopes as a comma-separated X-OAuth-Scopes header.
    return [scope.strip() for scope in header.split(",") if scope.strip()]


def _raise_for_github_status(response: httpx.Response, *, operation: str) -> None:
    if response.status_code < 400:
        return
    retry_after = response.headers.get("retry-after", "")
    rate_limited = response.status_code == 429 or (
        response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
    )
    if rate_limited:
        # Normalize primary and secondary rate limits to 429 so the router maps
        # them consistently and can echo Retry-After.
        raise GitHubUpstreamError(
            operation=operation,
            status_code=429,
            message="GitHub rate limit exceeded",
            retry_after=retry_after,
        )
    raise GitHubUpstreamError(
        operation=operation,
        status_code=response.status_code,
        message=_github_error_message(response),
        retry_after=retry_after,
    )


def _github_error_message(response: httpx.Response) -> str:
    """Return only GitHub's structured error message, never a raw body.

    A non-JSON body (e.g. a proxy's HTML error page) yields ``""`` so infra
    detail cannot leak into a 502.
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


def _fingerprint(*, query: str, repo: str, limit: int) -> str:
    raw = f"{query}\x1f{repo}\x1f{limit}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _encode_page_token(page: int, fingerprint: str) -> str:
    return f"{page}.{fingerprint}"


def _decode_page_token(page_token: str, fingerprint: str) -> int:
    if not page_token:
        return 1
    page_str, _, token_fingerprint = page_token.partition(".")
    if not token_fingerprint or token_fingerprint != fingerprint:
        raise GitHubSearchError(
            "page_token does not match the current search parameters; restart from the first page"
        )
    try:
        page = int(page_str)
    except ValueError as exc:
        raise GitHubSearchError("page_token is malformed") from exc
    if page < 1:
        raise GitHubSearchError("page_token is malformed")
    return page


def _repo_from_repository_url(repository_url: str) -> str:
    marker = "/repos/"
    index = repository_url.find(marker)
    if index == -1:
        return ""
    return repository_url[index + len(marker):].strip("/")


def _owner_node(data: dict) -> dict | None:
    """Return the organization or user node, whichever the GraphQL query resolved."""
    for key in ("organization", "user"):
        node = data.get(key)
        if isinstance(node, dict):
            return node
    return None


def _repo(node: dict) -> GitHubRepo:
    return GitHubRepo(
        full_name=str(node.get("full_name", "") or ""),
        description=sanitize_connector_text(str(node.get("description", "") or "")),
        open_issues=int(node.get("open_issues_count", 0) or 0),
        private=bool(node.get("private", False)),
        archived=bool(node.get("archived", False)),
        updated_at=node.get("updated_at"),
    )


def _project(node: dict) -> GitHubProject:
    return GitHubProject(
        number=int(node.get("number", 0) or 0),
        title=sanitize_connector_text(str(node.get("title", "") or "")),
        description=sanitize_connector_text(str(node.get("shortDescription", "") or "")),
        closed=bool(node.get("closed", False)),
        items_count=int((node.get("items") or {}).get("totalCount", 0) or 0),
    )


def _project_item(node: dict) -> GitHubProjectItem:
    content = node.get("content") if isinstance(node.get("content"), dict) else {}
    repository = content.get("repository") or {}
    repo = str(repository.get("nameWithOwner", "") or "") if isinstance(repository, dict) else ""
    status = ""
    fields: dict[str, str] = {}
    for value in ((node.get("fieldValues") or {}).get("nodes")) or []:
        if not isinstance(value, dict):
            continue
        field = value.get("field") if isinstance(value.get("field"), dict) else {}
        field_name = str(field.get("name", "") or "")
        raw = value.get("name")
        if raw is None:
            raw = value.get("text")
        if raw is None:
            raw = value.get("date")
        if raw is None and value.get("number") is not None:
            raw = value.get("number")
        if not field_name or raw in (None, ""):
            continue
        clean_name = sanitize_connector_text(field_name)
        clean_value = sanitize_connector_text(str(raw))
        if clean_name.lower() == "status":
            status = clean_value
        else:
            fields[clean_name] = clean_value
    return GitHubProjectItem(
        title=sanitize_connector_text(str(content.get("title", "") or "")),
        type=str(node.get("type", "") or ""),
        status=status,
        repo=repo,
        number=int(content.get("number", 0) or 0),
        fields=fields,
    )


def _logins(values: Any) -> list[str]:
    result: list[str] = []
    if isinstance(values, list):
        for entry in values:
            if isinstance(entry, dict):
                login = str(entry.get("login", "") or "")
                if login:
                    result.append(sanitize_connector_text(login))
    return result


def _label_names(values: Any) -> list[str]:
    result: list[str] = []
    if isinstance(values, list):
        for entry in values:
            name = str(entry.get("name", "") or "") if isinstance(entry, dict) else str(entry or "")
            if name:
                result.append(sanitize_connector_text(name))
    return result


def _reviews(values: Any) -> list[GitHubReview]:
    result: list[GitHubReview] = []
    if isinstance(values, list):
        for entry in values:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") or {}
            login = str(user.get("login", "") or "") if isinstance(user, dict) else ""
            state = str(entry.get("state", "") or "")
            if login or state:
                result.append(GitHubReview(user=sanitize_connector_text(login), state=state))
    return result


def _search_hit(item: dict) -> GitHubSearchHit:
    body = sanitize_connector_text(str(item.get("body", "") or ""))
    if len(body) > SEARCH_TEXT_SNIPPET_CHARS:
        body = body[:SEARCH_TEXT_SNIPPET_CHARS].rstrip() + "…"
    user = item.get("user") or {}
    return GitHubSearchHit(
        repo=_repo_from_repository_url(str(item.get("repository_url", "") or "")),
        number=int(item.get("number", 0) or 0),
        title=sanitize_connector_text(str(item.get("title", "") or "")),
        state=str(item.get("state", "") or ""),
        is_pull_request="pull_request" in item,
        author=str(user.get("login", "") or "") if isinstance(user, dict) else "",
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=int(item.get("comments", 0) or 0),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        text=body,
    )
