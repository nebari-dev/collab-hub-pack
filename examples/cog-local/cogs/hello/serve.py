"""hello: the smallest Cog worker. Standard library only.

A Cog worker is a process that serves two routes, the seam between the hub and
a Cog:

    GET  /healthz   200 once it can answer
    POST /invoke    {entry_point, input, idempotency_key} -> a result envelope

The run controller starts it with the package's `serve` task (see `pixi.toml`),
tells it where to listen (`COLLAB_COG_HOST`, `COLLAB_COG_PORT`), and hands it a
run token (`COLLAB_RUN_TOKEN`) that it presents on every `/invoke`.

This Cog has one entry point, `run`. Its input is `{"name": ..., "seconds": ...}`:
it waits `seconds` (none by default), then greets `name`.
"""

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COG = os.environ.get("COLLAB_COG_ID", "hello")
RUN = os.environ.get("COLLAB_RUN_ID", "")
TOKEN = os.environ.get("COLLAB_RUN_TOKEN", "")


def envelope(entry_point, *, payload=None, error=None):
    """A version-1 result envelope: what every Cog answers with."""
    return {
        "envelope": 1, "cog": {"id": COG, "version": "0.1.0"}, "task": entry_point,
        "ok": error is None, "error": error, "payload": payload,
        "raw": None, "problems": [], "binding": None, "usage": None,
    }


def run(value):
    value = value or {}
    time.sleep(float(value.get("seconds", 0)))
    return {"greeting": f"Hello, {value.get('name', 'world')}!", "run": RUN, "pid": os.getpid()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._send(200, {"ok": True}) if self.path == "/healthz" else self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/invoke":
            return self._send(404, {"error": "not found"})
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not TOKEN or not hmac.compare_digest(presented.encode(), TOKEN.encode()):
            return self._send(401, {"error": "not the run token of this worker"})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        entry_point = request.get("entry_point")
        if entry_point != "run":
            error = {"code": "invalid-input", "detail": f"hello has no entry point {entry_point!r}; it has `run`"}
            return self._send(422, envelope(entry_point, error=error))
        self._send(200, envelope(entry_point, payload=run(request.get("input"))))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    host, port = os.environ.get("COLLAB_COG_HOST", "127.0.0.1"), int(os.environ["COLLAB_COG_PORT"])
    print(f"hello: serving run {RUN} on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
