from __future__ import annotations

import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCESS_TOKEN = "fake-google-access-token"


def encoded_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


FILE_BODY = "Implemented healthcare analytics platform.\nReduced manual review by 30%."
FILE_METADATA = {
    "id": "file-1",
    "name": "Healthcare past performance",
    "mimeType": "application/vnd.google-apps.document",
    "modifiedTime": "2026-06-01T12:00:00Z",
    "webViewLink": "https://docs.google.test/file-1",
    "owners": [{"displayName": "Alice"}],
}
GMAIL_MESSAGE = {
    "id": "message-1",
    "threadId": "thread-1",
    "internalDate": "1783612800000",
    "snippet": "Finish the connector rollout checklist at https://unsafe.example.test/checklist.",
    "labelIds": ["INBOX"],
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Connector rollout"},
            {"name": "From", "value": "Mark <mark@example.com>"},
            {"name": "To", "value": "alice@example.com"},
        ],
        "mimeType": "text/plain",
        "filename": "",
        "body": {
            "data": encoded_body(
                "Finish the connector rollout checklist. "
                "See https://unsafe.example.test/checklist."
            ),
        },
    },
}
GMAIL_MESSAGE_2 = {
    **GMAIL_MESSAGE,
    "id": "message-2",
    "threadId": "thread-2",
    "snippet": "Second page follow-up.",
}
HISTORICAL_GMAIL_MESSAGE = {
    "id": "historical-message-2023",
    "threadId": "historical-thread-2023",
    "internalDate": "1676388600000",
    "snippet": "Zephyr renewal was approved in February 2023.",
    "labelIds": ["INBOX", "IMPORTANT"],
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Zephyr renewal decision — 2023"},
            {"name": "From", "value": "Mark <mark@example.com>"},
            {"name": "To", "value": "alice@example.com"},
            {"name": "Date", "value": "Tue, 14 Feb 2023 15:30:00 +0000"},
        ],
        "mimeType": "text/html",
        "filename": "",
        "body": {
            "data": encoded_body(
                "<div>Zephyr renewal was approved for two years.</div>"
                "<div>Mark owns the security addendum; Alice owns pricing.</div>"
                "<div>Original archive: https://unsafe.example.test/zephyr-2023</div>"
            )
        },
    },
}
HISTORICAL_GMAIL_MESSAGE_2 = {
    "id": "historical-message-2021",
    "threadId": "historical-thread-2021",
    "internalDate": "1633003200000",
    "snippet": "The original Zephyr kickoff assigned the migration checklist.",
    "labelIds": ["ARCHIVE"],
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Zephyr kickoff notes — 2021"},
            {"name": "From", "value": "Priya <priya@example.com>"},
            {"name": "To", "value": "alice@example.com"},
            {"name": "Date", "value": "Thu, 30 Sep 2021 12:00:00 +0000"},
        ],
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/plain",
                "filename": "",
                "body": {
                    "data": encoded_body(
                        "The 2021 Zephyr kickoff assigned the migration checklist to Alice."
                    )
                },
            },
            {
                "mimeType": "application/pdf",
                "filename": "confidential-zephyr.pdf",
                "body": {"data": encoded_body("PRIVATE ATTACHMENT CONTENT")},
            },
        ],
    },
}
CALENDAR_EVENT = {
    "id": "event-1",
    "summary": "Connector planning",
    "description": "Assign owners using https://unsafe.example.test/calendar-plan.",
    "start": {"dateTime": "2026-07-10T14:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-07-10T14:30:00-04:00", "timeZone": "America/New_York"},
    "status": "confirmed",
    "htmlLink": "https://unsafe.example.test/event-1",
}
CALENDAR_EVENT_2 = {
    **CALENDAR_EVENT,
    "id": "event-2",
    "summary": "Connector follow-up",
    "start": {"dateTime": "2026-07-11T14:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-07-11T14:30:00-04:00", "timeZone": "America/New_York"},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond_json({"ok": True})
            return
        if parsed.path == "/broker/token":
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self.respond_json({"error": "missing bearer"}, status=401)
                return
            self.respond_json({"access_token": ACCESS_TOKEN, "expires_in": 3600})
            return
        if self.headers.get("Authorization", "") != f"Bearer {ACCESS_TOKEN}":
            self.respond_json({"error": "missing fake google token"}, status=401)
            return
        if parsed.path == "/drive/v3/files":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if "healthcare" not in query:
                self.respond_json({"files": []})
                return
            self.respond_json({"files": [FILE_METADATA]})
            return
        if parsed.path == "/drive/v3/files/file-1":
            self.respond_json(FILE_METADATA)
            return
        if parsed.path == "/drive/v3/files/file-1/export":
            self.respond_text(FILE_BODY)
            return
        if parsed.path == "/gmail/v1/users/me/profile":
            self.respond_json({"emailAddress": "alice@example.com", "messagesTotal": 1})
            return
        if parsed.path == "/gmail/v1/users/me/messages":
            query = parse_qs(parsed.query)
            page_token = query.get("pageToken", [""])[0]
            gmail_query = query.get("q", [""])[0]
            if "apollo-history-proof" in gmail_query:
                parts = gmail_query.split()
                after = next((value.removeprefix("after:") for value in parts if value.startswith("after:")), "")
                before = next((value.removeprefix("before:") for value in parts if value.startswith("before:")), "")
                if not (after.isdigit() and before.isdigit() and int(after) < int(before)):
                    self.respond_json(
                        {"error": "historical search is missing date bounds"},
                        status=400,
                    )
                    return
                if page_token == "history-page-2":
                    self.respond_json(
                        {
                            "messages": [
                                {
                                    "id": "historical-message-2021",
                                    "threadId": "historical-thread-2021",
                                }
                            ],
                            "resultSizeEstimate": 2,
                        }
                    )
                    return
                self.respond_json(
                    {
                        "messages": [
                            {
                                "id": "historical-message-2023",
                                "threadId": "historical-thread-2023",
                            }
                        ],
                        "nextPageToken": "history-page-2",
                        "resultSizeEstimate": 2,
                    }
                )
                return
            if page_token == "gmail-page-2":
                self.respond_json(
                    {"messages": [{"id": "message-2", "threadId": "thread-2"}]}
                )
                return
            self.respond_json(
                {
                    "messages": [{"id": "message-1", "threadId": "thread-1"}],
                    "nextPageToken": "gmail-page-2",
                    "resultSizeEstimate": 2,
                }
            )
            return
        if parsed.path == "/gmail/v1/users/me/messages/message-1":
            self.respond_json(GMAIL_MESSAGE)
            return
        if parsed.path == "/gmail/v1/users/me/messages/message-2":
            self.respond_json(GMAIL_MESSAGE_2)
            return
        if parsed.path == "/gmail/v1/users/me/messages/historical-message-2023":
            self.respond_json(HISTORICAL_GMAIL_MESSAGE)
            return
        if parsed.path == "/gmail/v1/users/me/messages/historical-message-2021":
            self.respond_json(HISTORICAL_GMAIL_MESSAGE_2)
            return
        if parsed.path == "/calendar/v3/users/me/calendarList":
            self.respond_json(
                {
                    "items": [
                        {
                            "id": "primary",
                            "summary": "Work",
                            "primary": True,
                            "timeZone": "America/New_York",
                        }
                    ]
                }
            )
            return
        if parsed.path == "/calendar/v3/calendars/primary/events":
            page_token = parse_qs(parsed.query).get("pageToken", [""])[0]
            if page_token == "calendar-page-2":
                self.respond_json({"items": [CALENDAR_EVENT_2]})
                return
            self.respond_json(
                {"items": [CALENDAR_EVENT], "nextPageToken": "calendar-page-2"}
            )
            return
        if parsed.path == "/calendar/v3/calendars/primary/events/event-1":
            self.respond_json(CALENDAR_EVENT)
            return
        self.respond_json({"error": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def respond_json(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, payload: str, *, status: int = 200) -> None:
        body = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler
    ).serve_forever()
