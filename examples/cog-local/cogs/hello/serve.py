"""hello: the smallest Cog worker that holds a conversation. Standard library only.

A Cog worker is a process the run controller starts with the package's `serve`
task (see `pixi.toml`). The controller tells it where to listen
(`COLLAB_COG_HOST`, `COLLAB_COG_PORT`) and hands it a run token
(`COLLAB_RUN_TOKEN`), which the controller presents on every request. It serves:

    GET  /healthz   200 once it can answer
    POST /invoke    {entry_point, input, idempotency_key} -> a result envelope
    POST /turn      {turn, text} -> {text}, while a `session` is open

Two entry points:

- `run` greets `input.name` and answers at once.
- `session` holds a conversation: `/invoke` stays open, and each turn a client
  sends through the hub reaches `/turn`. It ends when someone says `bye`, or
  when the run is terminated.
"""

import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COG = os.environ.get("COLLAB_COG_ID", "hello")
RUN = os.environ.get("COLLAB_RUN_ID", "")
TOKEN = os.environ.get("COLLAB_RUN_TOKEN", "")

HELP = """I am **hello**, a Cog running as a local process. Try:

- `hello Ada` — a greeting
- `sum 1 2 3` — add numbers
- `time` — the time, here
- `whoami` — which run and process you are talking to
- `history` — what you said so far
- `bye` — end the session (the run then completes)"""


class Session:
    """One conversation: what was said, and whether it is over."""

    def __init__(self):
        self.said = []
        self.over = threading.Event()

    def answer(self, text):
        words = text.split()
        command, rest = (words[0].lower(), words[1:]) if words else ("help", [])
        self.said.append(text)
        if command in ("help", "?"):
            return HELP
        if command in ("hello", "hi"):
            return f"Hello, {' '.join(rest) or 'world'}!"
        if command == "sum":
            try:
                return f"{' + '.join(rest)} = {sum(float(n) for n in rest):g}"
            except ValueError:
                return "`sum` takes numbers, e.g. `sum 1 2 3`"
        if command == "time":
            return datetime.now(timezone.utc).strftime("It is %H:%M:%S UTC.")
        if command == "whoami":
            return f"Run `{RUN}`, Cog `{COG}`, process {os.getpid()}."
        if command == "history":
            return "\n".join(f"{n}. {line}" for n, line in enumerate(self.said[:-1], 1)) or "Nothing yet."
        if command in ("bye", "exit", "quit"):
            self.over.set()
            return "Bye! The session ends, and the run completes."
        return f"I do not know `{command}`. Say `help`."


session = None


def envelope(entry_point, *, payload=None, error=None):
    """A version-1 result envelope: what every Cog answers `/invoke` with."""
    return {
        "envelope": 1, "cog": {"id": COG, "version": "0.2.0"}, "task": entry_point,
        "ok": error is None, "error": error, "payload": payload,
        "raw": None, "problems": [], "binding": None, "usage": None,
    }


def invoke(entry_point, value):
    global session
    value = value or {}
    if entry_point == "run":
        time.sleep(float(value.get("seconds", 0)))
        return 200, envelope(entry_point, payload={"greeting": f"Hello, {value.get('name', 'world')}!",
                                                   "run": RUN, "pid": os.getpid()})
    if entry_point == "session":
        session = Session()
        session.over.wait()  # open until someone says bye; a terminated run kills the process instead
        return 200, envelope(entry_point, payload={"turns": len(session.said), "said": session.said})
    error = {"code": "invalid-input", "detail": f"hello has no entry point {entry_point!r}: it has run and session"}
    return 422, envelope(entry_point, error=error)


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
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not TOKEN or not hmac.compare_digest(presented.encode(), TOKEN.encode()):
            return self._send(401, {"error": "not the run token of this worker"})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/invoke":
            return self._send(*invoke(request.get("entry_point"), request.get("input")))
        if self.path == "/turn":
            if session is None or session.over.is_set():
                return self._send(404, {"error": "no session is open"})
            return self._send(200, {"text": session.answer(str(request.get("text", "")))})
        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    host, port = os.environ.get("COLLAB_COG_HOST", "127.0.0.1"), int(os.environ["COLLAB_COG_PORT"])
    print(f"hello: serving run {RUN} on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
