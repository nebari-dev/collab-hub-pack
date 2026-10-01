"""Standalone fake Notion API for local connector smoke tests (stdlib only).

Serves just enough of the read-only Notion surface the Collab Hub connector uses:

    GET  /v1/users/me
    POST /v1/search
    GET  /v1/pages/{id}
    GET  /v1/blocks/{id}/children
    POST /v1/databases/{id}/query
    GET  /broker/token           (Option A: Keycloak-style broker stand-in)

The seeded page carries a rich_text ``href`` and every object carries a ``url``
so smoke assertions can prove both are dropped. The search set needs a second
cursor page so pagination continuation is exercised. Bind loopback only.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOTION_VERSION = "2022-06-28"
BOT_TOKEN = "secret_fake-notion-bot-token"
WORKSPACE_NAME = "Fake Workspace"

PAGE_ID = "0123456789abcdef0123456789abcdef"
PAGE_ID_2 = "11111111111111111111111111111111"
DATABASE_ID = "fedcba9876543210fedcba9876543210"


def _title_property(text: str) -> dict:
    return {"Name": {"type": "title", "title": [{"plain_text": text, "href": None}]}}


def _page(page_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": last_edited,
        "properties": _title_property(title),
    }


def _database(database_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "database",
        "id": database_id,
        "url": f"https://www.notion.so/{database_id}",
        "last_edited_time": last_edited,
        "title": [{"plain_text": title, "href": None}],
    }


def _paragraph(text: str, href: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [{"plain_text": text, "href": href}]},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_version(self) -> bool:
        if self.headers.get("Notion-Version") != NOTION_VERSION:
            self._send(400, {"object": "error", "status": 400, "code": "missing_version", "message": "bad version"})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/broker/token":
            self._send(200, {"access_token": BOT_TOKEN, "token_type": "bearer"})
            return
        if not self._require_version():
            return
        if path == "/v1/users/me":
            self._send(200, {"object": "user", "type": "bot", "bot": {"workspace_name": WORKSPACE_NAME}})
            return
        if path == f"/v1/pages/{PAGE_ID}":
            self._send(200, _page(PAGE_ID, "Design Notes", "2026-02-10T00:00:00.000Z"))
            return
        if path == f"/v1/blocks/{PAGE_ID}/children":
            self._send(
                200,
                {
                    "object": "list",
                    "results": [
                        _paragraph("First line with a ", href="https://evil.example/should-be-dropped"),
                        _paragraph("Second line of the page."),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
            return
        self._send(404, {"object": "error", "status": 404, "code": "not_found", "message": "Not Found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._require_version():
            return
        body = self._body()
        if path == "/v1/search":
            cursor = body.get("start_cursor")
            if cursor is None:
                self._send(
                    200,
                    {
                        "object": "list",
                        "results": [_page(PAGE_ID, "Roadmap", "2026-02-10T00:00:00.000Z")],
                        "has_more": True,
                        "next_cursor": "cursor-2",
                    },
                )
            else:
                self._send(
                    200,
                    {
                        "object": "list",
                        "results": [_database(DATABASE_ID, "Tasks", "2026-02-01T00:00:00.000Z")],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
            return
        if path == f"/v1/databases/{DATABASE_ID}/query":
            self._send(
                200,
                {
                    "object": "list",
                    "results": [_page(PAGE_ID_2, "Row 1", "2026-02-05T00:00:00.000Z")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
            return
        self._send(404, {"object": "error", "status": 404, "code": "not_found", "message": "Not Found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Notion API for smoke tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8975)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake notion listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
