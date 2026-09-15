from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .connector_text import sanitize_connector_text
from .models import (
    GitHubItem,
    GitHubProject,
    GitHubProjectCounts,
    GitHubProjectItem,
    GitHubRepo,
    GitHubReview,
    GitHubSearchHit,
    GitHubStatusCount,
)

# GitHub search returns at most 1000 results across all pages, regardless of
# total_count, and caps per_page at 100. We never page past the 1000th result.
GITHUB_SEARCH_RESULT_CAP = 1000
GITHUB_MAX_PER_PAGE = 100
# Cap the free-text snippet carried in a search hit or a PR review body; a full
# issue/PR/review body belongs to a dedicated read, not a summary snippet. A PR
# keeps the newest MAX_ITEM_COMMENTS reviews, so this also bounds a PR's review
# text at MAX_ITEM_COMMENTS * TEXT_SNIPPET_CHARS regardless of body length.
TEXT_SNIPPET_CHARS = 500
# The contents API only returns files up to 1 MB; larger ones come back with
# encoding "none" and no content, and must be fetched via the git blobs API.
CONTENTS_MAX_BYTES = 1_000_000
# Bound how many comments a single item read pulls, independent of max_chars.
MAX_ITEM_COMMENTS = 30
# PR reviews come back oldest-first with no sort option; fetch a wide page and
# keep the newest tail so a later APPROVED isn't hidden by an early CHANGES_REQUESTED.
_PR_REVIEW_PAGE_SIZE = 100
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec"

logger = logging.getLogger("frames_server.connectors")

# Projects V2 are GraphQL-only. Query the org and user owners in one request and
# use whichever resolves, so one tool handles org and personal boards.
_PROJECT_ITEM_FIELDS = """
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          type
          content {
            __typename
            ... on Issue {
              number title state repository { nameWithOwner }
              assignees(first: 10) { nodes { login } }
              labels(first: 20) { nodes { name } }
            }
            ... on PullRequest {
              number title state repository { nameWithOwner }
              assignees(first: 10) { nodes { login } }
              labels(first: 20) { nodes { name } }
            }
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
query ProjectsList($login: String!, $first: Int!) {{
  organization(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
  user(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
}}
"""

_PROJECT_READ_BODY = f"""projectV2(number: $number) {{
      number title shortDescription closed
      items(first: $first, after: $after) {{ {_PROJECT_ITEM_FIELDS} }}
    }}"""

_PROJECT_READ_QUERY = f"""
query ProjectRead($login: String!, $number: Int!, $first: Int!, $after: String) {{
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

# --- Project board counts ---------------------------------------------------
# Projects V2 GraphQL has NO group-by aggregate; the only server-side counter is
# totalCount on the items connection. But items() accepts a board-filter `query`
# and an `archivedStates` argument, so one totalCount-only query per bucket gives
# an EXACT count regardless of the item-enumeration cap above. Every count query
# pins archivedStates:[NOT_ARCHIVED] so the buckets share one policy and the
# status columns provably reconcile to the total (archived_policy="excluded").
_COUNTS_ARCHIVED = "archivedStates: [NOT_ARCHIVED]"
# A1: options + total + by-type + by-state + blank-status, all in one request.
# The by_type/by_state aliases lean on the `query` filter DSL, so a rejection of
# that DSL surfaces here and drops the whole count path to the sampled fallback.
# `is:draft` matches BOTH board drafts (DraftIssue) and draft PRs, and a board
# draft ALSO matches `is:issue` -- so the type partition is built from the raw
# buckets, not by summing overlapping filters (see _authoritative_counts):
# `board_draft` (is:draft is:issue) isolates board drafts, letting issue/pr/draft
# form an exact, non-overlapping partition of the total.
_PROJECT_COUNTS_BODY = f"""projectV2(number: $number) {{
      statusField: field(name: "Status") {{ ... on ProjectV2SingleSelectField {{ options {{ name }} }} }}
      total: items(first: 0, {_COUNTS_ARCHIVED}) {{ totalCount }}
      issue: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:issue") {{ totalCount }}
      pull_request: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:pr") {{ totalCount }}
      board_draft: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:draft is:issue") {{ totalCount }}
      opened: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:open") {{ totalCount }}
      closed: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:closed") {{ totalCount }}
      no_status: items(first: 0, {_COUNTS_ARCHIVED}, query: "no:status") {{ totalCount }}
    }}"""

_PROJECT_COUNTS_QUERY = f"""
query ProjectCounts($login: String!, $number: Int!) {{
  organization(login: $login) {{ {_PROJECT_COUNTS_BODY} }}
  user(login: $login) {{ {_PROJECT_COUNTS_BODY} }}
}}
"""


def _status_counts_query(option_count: int) -> str:
    """Build one aliased query that counts every Status column in a single
    request. Column names are passed as GraphQL variables ($q0..$qN), never
    interpolated, so an option name cannot inject query text."""
    var_decls = "".join(f", $q{i}: String!" for i in range(option_count))
    aliases = "\n      ".join(
        f"s{i}: items(first: 0, {_COUNTS_ARCHIVED}, query: $q{i}) {{ totalCount }}"
        for i in range(option_count)
    )
    body = f"projectV2(number: $number) {{\n      {aliases}\n    }}"
    return (
        f"query ProjectStatusCounts($login: String!, $number: Int!{var_decls}) {{\n"
        f"  organization(login: $login) {{ {body} }}\n"
        f"  user(login: $login) {{ {body} }}\n"
        f"}}\n"
    )


def _project_filter_value(value: str) -> str:
    """Escape a value embedded in a quoted Projects filter expression.

    GraphQL variables keep option names out of the GraphQL document, but the
    variable itself is interpreted by GitHub's Projects filter DSL. Escape its
    string delimiters so a board-controlled Status option cannot change the
    query that is counted.

    Unverified upstream: live testing shows a quoted value with these escapes
    returns `totalCount: 0` with no error, so whether the Projects filter DSL
    actually honors backslash escapes (vs. silently never matching) could not
    be confirmed. A status option containing `"` in its name may therefore
    silently count as 0 rather than raising.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
    ):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

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
                # requested_teams entries always carry a "name", so the label
                # extractor (name-or-str) handles them without a slug fallback.
                item.requested_teams = _label_names(pull.get("requested_teams"))
                item.is_draft = bool(pull.get("draft"))
                item.merge_state = _merge_state(pull)
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
        return item, _snippet(full, max_chars), truncated

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
        return GitHubFileContent(content=_snippet(text, max_chars), truncated=truncated)

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
            project = _owner_project(
                data, operation="project read", status_code=404, message="project not found"
            )
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

    async def read_project_with_counts(
        self, *, owner: str, number: int, max_items: int
    ) -> tuple[GitHubProject, list[GitHubProjectItem], GitHubProjectCounts]:
        """Read a board and compute its aggregate counts concurrently.

        The exact server-side count queries don't depend on the item
        enumeration, so they run alongside read_project instead of strictly
        after it -- overlapping the two count round-trips with the 1-5
        enumeration pages rather than paying them in series.

        Counts never break the read: on any count failure (rejected filter DSL,
        non-reconciling self-check, upstream/network error, or a coding bug) we
        fall back to bucketing the enumerated items, flagged non-authoritative.
        A read_project failure, by contrast, propagates -- it's the caller's
        error to map -- after the counts task is cancelled so it can't leak."""
        counts_task = asyncio.ensure_future(
            self._authoritative_counts(login=owner.strip(), number=number)
        )
        try:
            project, items = await self.read_project(owner=owner, number=number, max_items=max_items)
        except BaseException:
            counts_task.cancel()
            with contextlib.suppress(BaseException):
                await counts_task
            raise
        total = project.items_count
        try:
            counts = await counts_task
        except (GitHubUpstreamError, httpx.HTTPError):
            # Expected degrade: upstream/network failure, a rejected filter DSL,
            # or a self-check that refused to stamp non-reconciling counts
            # authoritative (see _authoritative_counts). Sample at WARNING.
            logger.warning("github_project_counts_fallback", exc_info=True)
            counts = _sampled_counts(total=total, items=items)
        except Exception:
            # Unexpected: a coding bug (bad query edit, upstream shape change,
            # TypeError in parsing) would otherwise become permanent silent
            # degradation -- and a small fully-enumerated board still returns
            # authoritative=True from the fallback, masking it. Log at ERROR so a
            # shipped regression is loud, but still honor "must not break the
            # read" (asyncio.CancelledError is a BaseException and still surfaces).
            logger.error("github_project_counts_unexpected_error", exc_info=True)
            counts = _sampled_counts(total=total, items=items)
        return project, items, counts

    async def _authoritative_counts(self, *, login: str, number: int) -> GitHubProjectCounts:
        data = await self._graphql(
            _PROJECT_COUNTS_QUERY, {"login": login, "number": number}, operation="project counts"
        )
        # Existence was already confirmed by read_project moments earlier in the
        # same request, so a null projectV2 here is a partial GraphQL error on
        # this specific query (rate limit, transient upstream issue, ...), not
        # evidence the project doesn't exist -- no status_code, matching the
        # "invalid response" convention for an unexpected shape (vs. a real HTTP
        # error). project_counts turns this into the sampled fallback.
        project = _owner_project(
            data, operation="project counts", status_code=None, message="project data missing from response"
        )

        def _tc(key: str) -> int:
            return int((project.get(key) or {}).get("totalCount", 0) or 0)

        total = _tc("total")
        # is:draft matches BOTH board drafts and draft PRs, and a board draft also
        # matches is:issue, so the raw filters overlap. board_draft (is:draft
        # is:issue) isolates board drafts; subtracting it from is:issue leaves
        # real issues, draft PRs stay under is:pr, and the three buckets then
        # partition the total exactly -- redacted absorbs items matching none.
        board_draft = _tc("board_draft")
        issue = _tc("issue") - board_draft
        pull_request = _tc("pull_request")
        redacted = total - (issue + pull_request + board_draft)
        status_field = project.get("statusField") or {}
        options = [
            str((o or {}).get("name", "") or "")
            for o in (status_field.get("options") or [])
            if isinstance(o, dict)
        ]
        options = [o for o in options if o]
        by_status = await self._status_column_counts(login=login, number=number, options=options)
        no_status = _tc("no_status")
        # Verify the invariant before advertising authoritative=True. Two disclosed
        # edges can silently miscount while the query still "succeeds": a Status
        # option whose name contains `"` (see _project_filter_value), and a board
        # whose single-select isn't named "Status" (statusField -> null, so
        # by_status is empty while no:status counts something unrelated). If the
        # buckets don't reconcile to the total, refuse the authoritative stamp --
        # raise so project_counts degrades to the honest sampled fallback.
        if sum(c.count for c in by_status) + no_status != total or issue < 0 or redacted < 0:
            raise GitHubUpstreamError(
                operation="project counts",
                message="server-side counts did not reconcile to the board total",
            )
        return GitHubProjectCounts(
            total=total,
            archived_policy="excluded",
            by_status=by_status,
            no_status=no_status,
            by_type={
                "issue": issue,
                "pull_request": pull_request,
                "draft": board_draft,
                # Derived: items matching none of the type filters (redacted content).
                "redacted": redacted,
            },
            by_state={"open": _tc("opened"), "closed": _tc("closed")},
            authoritative=True,
            counted_items=total,
        )

    async def _status_column_counts(
        self, *, login: str, number: int, options: list[str]
    ) -> list[GitHubStatusCount]:
        if not options:
            return []
        variables: dict[str, str | int] = {"login": login, "number": number}
        for index, option in enumerate(options):
            variables[f"q{index}"] = f'status:"{_project_filter_value(option)}"'
        data = await self._graphql(
            _status_counts_query(len(options)), variables, operation="project status counts"
        )
        # Same reasoning as _authoritative_counts: existence was already confirmed
        # by read_project, so a null projectV2 here is a partial GraphQL error on
        # this specific query, not a real not-found -- no status_code, so the
        # fallback's logged exception doesn't read as "HTTP 404" during, say, a
        # rate-limit incident.
        project = _owner_project(
            data,
            operation="project status counts",
            status_code=None,
            message="project data missing from response",
        )
        return [
            GitHubStatusCount(
                name=sanitize_connector_text(option),
                count=int((project.get(f"s{index}") or {}).get("totalCount", 0) or 0),
            )
            for index, option in enumerate(options)
        ]

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


def _snippet(text: str, limit: int) -> str:
    """Truncate to `limit` chars on an overflow, trimming trailing space and
    marking the cut with an ellipsis; return the text unchanged when it fits."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


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


def _owner_project(data: dict, *, operation: str, status_code: int | None, message: str) -> dict:
    """Resolve the projectV2 node from an org-or-user GraphQL response, raising a
    GitHubUpstreamError with the caller's message/status when it is absent."""
    node = _owner_node(data)
    project = node.get("projectV2") if node else None
    if not isinstance(project, dict):
        raise GitHubUpstreamError(operation=operation, status_code=status_code, message=message)
    return project


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
        state=str(content.get("state", "") or "").lower(),
        repo=repo,
        number=int(content.get("number", 0) or 0),
        assignees=_logins((content.get("assignees") or {}).get("nodes")),
        labels=_label_names((content.get("labels") or {}).get("nodes")),
        fields=fields,
    )


_PROJECT_TYPE_BUCKETS = {
    "ISSUE": "issue",
    "PULL_REQUEST": "pull_request",
    "DRAFT_ISSUE": "draft",
    "REDACTED": "redacted",
}


def _sampled_counts(*, total: int, items: list[GitHubProjectItem]) -> GitHubProjectCounts:
    """Bucket the already-enumerated items when server-side counts are
    unavailable. Exact only when the whole board was enumerated
    (counted == total); otherwise flagged non-authoritative so a caller never
    reads a truncated sample as a full count."""
    by_status: dict[str, int] = {}
    order: list[str] = []
    no_status = 0
    by_type = {"issue": 0, "pull_request": 0, "draft": 0, "redacted": 0}
    by_state = {"open": 0, "closed": 0}
    for item in items:
        bucket = _PROJECT_TYPE_BUCKETS.get(item.type)
        if bucket:
            by_type[bucket] += 1
        if item.status:
            if item.status not in by_status:
                by_status[item.status] = 0
                order.append(item.status)
            by_status[item.status] += 1
        else:
            no_status += 1
        if item.state == "merged":
            # A merged PR is closed; the authoritative path counts it under
            # is:closed, so sample the same way rather than dropping it.
            by_state["closed"] += 1
        elif item.state in ("open", "closed"):
            by_state[item.state] += 1
    counted = len(items)
    return GitHubProjectCounts(
        total=total,
        archived_policy="excluded",
        by_status=[GitHubStatusCount(name=name, count=by_status[name]) for name in order],
        no_status=no_status,
        by_type=by_type,
        by_state=by_state,
        authoritative=counted == total,
        counted_items=counted,
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


def _merge_state(pull: dict) -> str:
    """Collapse a PR's outcome to merged | closed_unmerged | open, so a closed
    PR is never ambiguous about whether it actually landed."""
    if pull.get("merged"):
        return "merged"
    if str(pull.get("state", "") or "").lower() == "closed":
        return "closed_unmerged"
    return "open"


def _reviews(values: Any) -> list[GitHubReview]:
    result: list[GitHubReview] = []
    if isinstance(values, list):
        for entry in values:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") or {}
            login = str(user.get("login", "") or "") if isinstance(user, dict) else ""
            state = str(entry.get("state", "") or "")
            body = _snippet(sanitize_connector_text(str(entry.get("body", "") or "")), TEXT_SNIPPET_CHARS)
            if login or state or body:
                result.append(GitHubReview(user=sanitize_connector_text(login), state=state, body=body))
    return result


def _search_hit(item: dict) -> GitHubSearchHit:
    body = _snippet(sanitize_connector_text(str(item.get("body", "") or "")), TEXT_SNIPPET_CHARS)
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
