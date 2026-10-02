"""The worker of a fake Cog: serves the seam for one `handle` function. Standard library only.

A fake Cog's `serve.py` defines `handle(entry_point, value, **feedback)`, which
returns a result envelope (`envelope()` below builds one), and ends with
`serve(handle)`. Served, it is what any Cog worker is to the controller:

    GET  /healthz   200 once it can answer
    POST /invoke    {entry_point, input, idempotency_key, signal?} -> a result envelope

It listens where the controller tells it to, `COLLAB_COG_HOST` and
`COLLAB_COG_PORT`, and answers `/invoke` only to the bearer of the run token in
its environment, `COLLAB_RUN_TOKEN`. `make op` without a location calls `handle`
in its own process instead, and never starts this server.
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COG = os.environ.get("COLLAB_COG_ID", "unknown")


def envelope(payload=None, *, ok=True, error=None, problems=(), usage=None):
    """A version-1 result envelope (docs/cog-execution/result-envelope.md)."""
    return {
        "envelope": 1, "cog": {"id": COG, "version": "0.0.0"}, "task": None, "ok": ok, "error": error,
        "payload": payload if ok else None, "raw": None, "problems": list(problems), "binding": None, "usage": usage,
    }


def serve(handle) -> None:
    token = os.environ.get("COLLAB_RUN_TOKEN", "")

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
                self._send(200, {"ok": True, "cog": COG})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/invoke":
                self._send(404, {"error": "not found"})
                return
            presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not token or not hmac.compare_digest(presented.encode(), token.encode()):
                self._send(401, {"error": "not the run token of this worker"})
                return
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            feedback = {"signal": request["signal"]} if "signal" in request else {}
            answer = handle(request.get("entry_point"), request.get("input"), **feedback)
            self._send(200, {**answer, "task": request.get("entry_point")})

        def log_message(self, *args) -> None:  # the access log is noise in a run's stderr
            pass

    host, port = os.environ.get("COLLAB_COG_HOST", "127.0.0.1"), int(os.environ["COLLAB_COG_PORT"])
    print(f"{COG}: serving on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
