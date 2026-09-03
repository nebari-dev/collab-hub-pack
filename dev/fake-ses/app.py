"""Fake Amazon SES v2 for local development.

Implements the one call the Collab Hub API makes -- ``SendEmail``
(``POST /v2/email/outbound-emails``) -- and relays the message over SMTP into
Mailpit so the invitation link can be clicked. Everything else answers 404.

LocalStack's free tier does not emulate SES v2, hence this ~100-line
stand-in. It runs on the stock ``python:3.14-slim`` image with no
dependencies (see dev/docker-compose.yaml).
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import uuid
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SMTP_HOST = os.environ.get("SMTP_HOST", "mailpit")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
LISTEN_PORT = int(os.environ.get("PORT", "8080"))


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


def _build_message(payload: dict) -> EmailMessage:
    simple = payload["Content"]["Simple"]
    message = EmailMessage()
    message["From"] = payload["FromEmailAddress"]
    message["To"] = ", ".join(payload["Destination"]["ToAddresses"])
    message["Subject"] = simple["Subject"]["Data"]
    for header in simple.get("Headers", []):
        message[header["Name"]] = header["Value"]
    if configuration_set := payload.get("ConfigurationSetName"):
        message["X-SES-CONFIGURATION-SET"] = configuration_set
    if tags := payload.get("EmailTags"):
        message["X-SES-MESSAGE-TAGS"] = ", ".join(f"{t['Name']}={t['Value']}" for t in tags)
    body = simple["Body"]
    if "Text" in body:
        message.set_content(body["Text"]["Data"])
    if "Html" in body:
        message.add_alternative(body["Html"]["Data"], subtype="html")
    return message


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-sesv2/1"

    def _respond(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"message": "not emulated", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/v2/email/outbound-emails":
            self._respond(404, {"message": "not emulated", "path": self.path})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length))
            message = _build_message(payload)
        except (KeyError, TypeError, ValueError) as exc:
            self._respond(400, {"__type": "BadRequestException", "message": f"unsupported SendEmail shape: {exc}"})
            return
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            _log("relay_failed", error=str(exc))
            self._respond(500, {"__type": "InternalFailure", "message": f"SMTP relay failed: {exc}"})
            return
        message_id = uuid.uuid4().hex
        _log("sent", to=payload["Destination"]["ToAddresses"], subject=message["Subject"], message_id=message_id)
        self._respond(200, {"MessageId": message_id})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server API
        _log("request", line=format % args)


if __name__ == "__main__":
    _log("listening", port=LISTEN_PORT, smtp=f"{SMTP_HOST}:{SMTP_PORT}")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
