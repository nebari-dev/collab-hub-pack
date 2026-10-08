"""A model that orders a shell command: for checking that the Hermes Cog runs none.

    python tool_ordering_model.py PORT MARKER_PATH

Asked anything, it answers with a call to Hermes's `terminal` tool that would
create MARKER_PATH. Given the tool's result, it repeats it. It records the
tools it was offered in its stderr.
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, MARKER = int(sys.argv[1]), sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        data = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages") or []
        print(f"offered: {[tool['function']['name'] for tool in body.get('tools') or []]}", file=sys.stderr, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "x", "object": "chat.completion.chunk", "created": int(time.time()), "model": "fake-model"}

        def send(choice):
            self.wfile.write(f"data: {json.dumps({**chunk, 'choices': [choice]})}\n\n".encode())
            self.wfile.flush()

        if messages and messages[-1].get("role") == "tool":
            send({"index": 0, "delta": {"role": "assistant", "content": f"tool said: {messages[-1]['content']}"},
                  "finish_reason": None})
            send({"index": 0, "delta": {}, "finish_reason": "stop"})
        else:
            call = {"index": 0, "id": "call-1", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": f"touch {MARKER}"})}}
            send({"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None})
            send({"index": 0, "delta": {}, "finish_reason": "tool_calls"})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
