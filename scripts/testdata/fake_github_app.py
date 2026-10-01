from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Stand-in for Keycloak's GitHub broker plus the GitHub REST and GraphQL calls
# the connector makes. Stdlib only, like the Google and Slack fakes, so there is
# no image to build and `docker compose` can run it from the source directory.
#
# The scopes below are the ones docs/github-connector.md asks the identity
# provider for. They are reported through the `X-OAuth-Scopes` header on
# /user, because that is where the connector reads a token's *real* grant --
# reporting the configured intention instead would hide a stale link.

ACCESS_TOKEN = "fake-github-access-token"
OAUTH_SCOPES = "user:email, repo, read:org, read:project"
LOGIN = "dev"
OWNER_REPO = "nebari-dev/collab-hub-pack"

REPO = {
    "name": "collab-hub-pack",
    "full_name": OWNER_REPO,
    "private": False,
    "html_url": f"https://github.test/{OWNER_REPO}",
    "description": "Collab Hub, the pack",
}

ISSUE = {
    "number": 42,
    "title": "Frames should keep their orbits when withdrawn",
    "state": "open",
    "user": {"login": "alice"},
    "assignees": [{"login": "bob"}],
    "labels": [{"name": "frames"}, {"name": "beta"}],
    "comments": 3,
    "created_at": "2026-08-01T09:00:00Z",
    "updated_at": "2026-09-01T12:30:00Z",
    "repository_url": f"https://api.github.test/repos/{OWNER_REPO}",
    # Carries a link so sanitization is exercised the same way the Slack fake
    # does it: the read response must come back link-free.
    "body": "Withdrawing should keep them. Notes at https://example.test/orbits",
}

PULL = {
    **ISSUE,
    "number": 43,
    "title": "Keep orbits across a withdrawal",
    "pull_request": {"html_url": f"https://github.test/{OWNER_REPO}/pull/43"},
    "merged": False,
    "draft": False,
    "body": "Implements the above.",
}

# read_item fetches the issue, then its comments, and for a pull request its
# reviews as well. All three have to answer or the read fails as a 404.
COMMENTS = [
    {
        "id": 1,
        "user": {"login": "bob"},
        "created_at": "2026-08-02T10:00:00Z",
        "body": "Agreed. Orbits are the accumulated signal, not a property of being listed.",
    }
]

REVIEWS = [
    {"id": 1, "user": {"login": "carol"}, "state": "APPROVED", "body": "Reads well."}
]

FILE_CONTENT = "# Collab Hub\n\nA Nebari software pack.\n"

PROJECT_ITEMS = {
    "data": {
        "organization": {
            "projectV2": {
                "number": 7,
                "title": "Collab A4 Release",
                "shortDescription": "What ships in A4",
                "items": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PVTI_1",
                            "fieldValues": {
                                "nodes": [
                                    {
                                        "name": "In progress",
                                        "field": {"name": "Status"},
                                    }
                                ]
                            },
                            "content": {
                                "__typename": "Issue",
                                "number": ISSUE["number"],
                                "title": ISSUE["title"],
                                "url": f"https://github.test/{OWNER_REPO}/issues/42",
                                "repository": {"nameWithOwner": OWNER_REPO},
                                "assignees": {"nodes": [{"login": "bob"}]},
                                "labels": {"nodes": [{"name": "frames"}]},
                            },
                        }
                    ],
                },
            }
        }
    }
}

PROJECT_LIST = {
    "data": {
        "organization": {
            "projectsV2": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "number": 7,
                        "title": "Collab A4 Release",
                        "shortDescription": "What ships in A4",
                        "url": "https://github.test/orgs/nebari-dev/projects/7",
                        "closed": False,
                        "items": {"totalCount": 1},
                    }
                ],
            }
        }
    }
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self.respond_json({"ok": True})
            return

        if path == "/broker/token":
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self.respond_json({"error": "missing bearer"}, status=401)
                return
            self.respond_json({"access_token": ACCESS_TOKEN, "expires_in": 3600})
            return

        if not self.authorized():
            return

        # The status probe: /user validates the token and carries the granted
        # scopes, /user/repos proves the token can enumerate repositories.
        if path == "/api/user":
            self.respond_json(
                {"login": LOGIN, "name": "Dev User"},
                extra_headers={"X-OAuth-Scopes": OAUTH_SCOPES},
            )
            return
        if path == "/api/user/repos":
            self.respond_json([REPO])
            return

        if path in (f"/api/orgs/{OWNER_REPO.split('/')[0]}/repos", f"/api/users/{LOGIN}/repos"):
            self.respond_json([REPO])
            return

        if path == "/api/search/issues":
            text = " ".join(query.get("q", [""]))
            items = [ISSUE, PULL] if "healthcare" not in text else []
            self.respond_json({"total_count": len(items), "incomplete_results": False, "items": items})
            return

        if path == f"/api/repos/{OWNER_REPO}/issues/{ISSUE['number']}/comments":
            self.respond_json(COMMENTS)
            return
        if path == f"/api/repos/{OWNER_REPO}/issues/{PULL['number']}/comments":
            self.respond_json(COMMENTS)
            return
        if path == f"/api/repos/{OWNER_REPO}/pulls/{PULL['number']}/reviews":
            self.respond_json(REVIEWS)
            return

        if path == f"/api/repos/{OWNER_REPO}/issues/{ISSUE['number']}":
            self.respond_json(ISSUE)
            return
        if path == f"/api/repos/{OWNER_REPO}/issues/{PULL['number']}":
            self.respond_json(PULL)
            return
        if path == f"/api/repos/{OWNER_REPO}/pulls/{PULL['number']}":
            self.respond_json(PULL)
            return

        if path.startswith(f"/api/repos/{OWNER_REPO}/contents/"):
            name = path.rsplit("/", 1)[-1]
            self.respond_json(
                {
                    "type": "file",
                    "name": name,
                    "path": path.split("/contents/", 1)[-1],
                    "size": len(FILE_CONTENT),
                    "encoding": "base64",
                    "content": base64.b64encode(FILE_CONTENT.encode()).decode(),
                }
            )
            return

        self.respond_json({"message": "Not Found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/graphql":
            self.respond_json({"message": "Not Found"}, status=404)
            return
        if not self.authorized():
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            query = str(json.loads(raw).get("query", ""))
        except ValueError:
            query = ""
        # One board listing, one board read -- told apart by which field the
        # caller asked for, the same way the real GraphQL API would.
        self.respond_json(PROJECT_LIST if "projectsV2(" in query else PROJECT_ITEMS)

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header in (f"Bearer {ACCESS_TOKEN}", f"token {ACCESS_TOKEN}"):
            return True
        self.respond_json({"message": "Bad credentials"}, status=401)
        return False

    def log_message(self, format: str, *args) -> None:
        return

    def respond_json(self, payload, *, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import os

    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
