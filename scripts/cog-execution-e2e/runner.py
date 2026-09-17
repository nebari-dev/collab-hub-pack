"""Minimal Cog worker for the kind E2E: serves /healthz and /invoke (stdlib only).

/invoke answers with a version-1 result envelope
(docs/cog-execution/result-envelope.md): ``payload`` echoes the input, ``usage``
reports tokens, ``problems`` is empty.

Pause fixture: a Cog whose COG_ID contains "gated" pauses until a separate
signal carries ``{"approved": true}``. This exercises transport and recovery;
it does not implement the Op-owned Gate policy defined in docs/GLOSSARY.md, and
it leaves once Gates are declared on the step (#99).
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

COG_ID = os.environ.get("COG_ID", "unknown")
GATED = "gated" in COG_ID
# Idempotency: a replayed key returns the prior result without repeating the side
# effect. In-process here (a fresh pod starts empty); a real Cog persists this.
_SEEN: dict[str, dict] = {}


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
        key = payload.get("idempotency_key")
        signal = payload.get("signal")
        approved = isinstance(signal, dict) and signal.get("approved") is True
        if GATED and not approved:
            self._send(200, {"pause": True, "reason": f"{COG_ID} awaiting approval", "usage": {"tokens": 0}})
            return
        if key is not None and key in _SEEN:  # replayed key -> no repeated side effect
            self._send(200, _SEEN[key])
            return
        result = {
            "envelope": 1,
            "cog": {"id": COG_ID, "version": "0.0.0"},
            "task": entry,
            "ok": True,
            "error": None,
            "payload": {"cog": COG_ID, "entry_point": entry, "echo": value, "signal": signal},
            "raw": None,
            "problems": [],
            "binding": None,
            "usage": {"tokens": 10},
        }
        if key is not None:
            _SEEN[key] = result
        self._send(200, result)

    def log_message(self, *args) -> None:  # quiet
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
