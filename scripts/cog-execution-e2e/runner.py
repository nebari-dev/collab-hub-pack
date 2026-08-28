"""Minimal Cog worker for the kind E2E: serves /healthz and /invoke (stdlib only).

Gate behavior: a Cog whose COG_ID contains "gated" pauses until the input is an
approval (``{"approved": true}``); any other Cog echoes an output. This lets the
E2E exercise a real gate -> approve -> complete loop against materialized pods.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

COG_ID = os.environ.get("COG_ID", "unknown")
GATED = "gated" in COG_ID


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True, "cog": COG_ID})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invoke":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        entry, value = payload.get("entry_point"), payload.get("input")
        approved = isinstance(value, dict) and value.get("approved") is True
        if GATED and not approved:
            self._send(200, {"pause": True, "reason": f"{COG_ID} awaiting approval"})
        else:
            self._send(
                200,
                {"output": {"cog": COG_ID, "entry_point": entry, "echo": value, "usage": {"tokens": 10}}},
            )

    def log_message(self, *args) -> None:  # quiet
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
