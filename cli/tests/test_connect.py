"""Talking to a running Cog: `run say`, and `run connect`, the run as an ACP agent for a client such as Toad."""

from __future__ import annotations

import io
import json
import os
import threading

import pytest

from collab_hub_cli import acp, main
from collab_hub_cli.config import resolve
from collab_hub_cli.hub import Hub

from .conftest import HUB


@pytest.fixture(autouse=True)
def dev_hub(stub, monkeypatch):
    stub.dev_auth = True
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    return stub


def _launched(stub, cli, cog="echo"):
    assert cli("--hub", HUB, "cog", "launch", cog).exit_code == 0
    stub.runs[0]["status"] = "RUNNING"
    return stub.runs[0]["id"]


def test_run_say_sends_one_turn_and_prints_the_answer(stub, cli):
    run_id = _launched(stub, cli)
    said = cli("--hub", HUB, "run", "say", run_id, "sum", "1", "2")
    assert said.exit_code == 0, said.output
    assert said.stdout.strip() == "you said: sum 1 2"
    assert json.loads(cli("--hub", HUB, "run", "say", run_id, "time", "--json").stdout)["state"] == "answered"


def test_run_say_reports_a_turn_the_cog_did_not_answer_and_a_run_that_ended(stub, cli):
    run_id = _launched(stub, cli)
    failed = cli("--hub", HUB, "run", "say", run_id, "fail")
    assert failed.exit_code == 1 and "the Cog did not answer: the worker answered HTTP 500" in failed.stderr
    stub.runs[0].update(status="COMPLETED", ended=True)
    ended = cli("--hub", HUB, "run", "say", run_id, "hello")
    assert ended.exit_code == 1 and "takes no turns: the run has ended COMPLETED (HTTP 409)" in ended.stderr


def test_cog_list_launchable_names_what_the_hub_can_launch(stub, cli):
    listed = cli("--hub", HUB, "cog", "list", "--launchable")
    assert listed.exit_code == 0 and listed.stdout.split() == ["echo", "slow"]
    assert json.loads(cli("--hub", HUB, "cog", "list", "--launchable", "--json").stdout) == ["echo", "slow"]
    stub.launchable = ()
    assert "launches no Cogs" in cli("--hub", HUB, "cog", "list", "--launchable").stderr


class Client:
    """What an ACP client does over the agent's stdin and stdout: one JSON-RPC message per line."""

    def __init__(self, run_id, tmp_path):
        self.hub = Hub(resolve(HUB, None))
        self.read_fd, self.write_fd = os.pipe()
        self.stdin = os.fdopen(self.read_fd, "r")
        self.feed = os.fdopen(self.write_fd, "w")
        self.out = io.StringIO()
        self.bridge = acp.Bridge(self.hub, run_id, self.stdin, self.out, poll_seconds=0.01)
        self.thread = threading.Thread(target=self.bridge.serve, daemon=True)
        self.thread.start()
        self.next_id = 0

    def send(self, method, params=None, *, notify=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.feed.write(json.dumps(message) + "\n")
        self.feed.flush()
        return message.get("id")

    def answered(self, request_id):
        """Wait until the agent has answered a request."""
        for _ in range(500):
            if any(json.loads(line).get("id") == request_id for line in self.out.getvalue().splitlines()):
                return
            threading.Event().wait(0.01)
        raise AssertionError(f"request {request_id} was never answered")

    def close(self):
        self.feed.close()
        self.thread.join(5)
        assert not self.thread.is_alive()
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


def test_run_connect_is_an_acp_agent_whose_prompts_are_turns_of_the_run(stub, cli, tmp_path, monkeypatch):
    run_id = _launched(stub, cli)
    client = Client(run_id, tmp_path)
    client.send("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True},
                                                                            "terminal": True},
                               "clientInfo": {"name": "toad", "version": "0"}})
    client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    client.send("session/prompt", {"sessionId": f"{run_id}-1", "prompt": [{"type": "text", "text": "sum 1 2"}]})
    client.send("session/load", {"sessionId": "x", "cwd": "/", "mcpServers": []})
    client.send("session/prompt", {"sessionId": "nope", "prompt": [{"type": "text", "text": "hi"}]})
    client.send("not json at all", notify=True)
    messages = client.close()
    by_id = {message["id"]: message for message in messages if "id" in message}
    initialized = by_id[1]["result"]
    assert initialized["protocolVersion"] == 1 and initialized["authMethods"] == []
    assert initialized["agentCapabilities"]["loadSession"] is False
    assert by_id[2]["result"] == {"sessionId": f"{run_id}-1"}
    assert by_id[3]["result"] == {"sessionId": f"{run_id}-1", "stopReason": "end_turn"}
    assert by_id[4]["error"]["code"] == acp.METHOD_NOT_FOUND
    assert by_id[5]["error"]["code"] == acp.INVALID_PARAMS
    [update] = [message for message in messages if message.get("method") == "session/update"]
    assert update["params"] == {"sessionId": f"{run_id}-1", "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "you said: sum 1 2"}}}
    # The answer is streamed before the prompt's result, as a client expects.
    assert messages.index(update) < messages.index(by_id[3])
    assert [turn["text"] for turn in stub.turns] == ["sum 1 2"]


def test_a_cancelled_prompt_stops_waiting_and_says_so(stub, cli, tmp_path):
    run_id = _launched(stub, cli)
    stub.hold_turns = True
    client = Client(run_id, tmp_path)
    client.send("initialize", {"protocolVersion": 1})
    client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    client.send("session/prompt", {"sessionId": f"{run_id}-1", "prompt": [{"type": "text", "text": "slow"}]})
    while not stub.turns:
        threading.Event().wait(0.01)
    client.send("session/cancel", {"sessionId": f"{run_id}-1"}, notify=True)
    by_id = {message["id"]: message for message in client.close() if "id" in message}
    assert by_id[3]["result"]["stopReason"] == "cancelled"


def test_a_prompt_the_hub_refuses_is_told_to_the_client_and_ends_the_turn(stub, cli, tmp_path):
    run_id = _launched(stub, cli)
    client = Client(run_id, tmp_path)
    client.send("initialize", {"protocolVersion": 1})
    client.answered(client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []}))
    stub.runs[0].update(status="COMPLETED", ended=True)
    # One prompt at a time, as a client sends them: each waits for the one before to be answered.
    client.answered(client.send("session/prompt", {"sessionId": f"{run_id}-1",
                                                   "prompt": [{"type": "text", "text": "hi"}]}))
    client.send("session/prompt", {"sessionId": f"{run_id}-1", "prompt": [{"type": "image", "data": ""}]})
    messages = client.close()
    texts = [m["params"]["update"]["content"]["text"] for m in messages if m.get("method") == "session/update"]
    assert texts[0].startswith("The hub could not deliver this: Run") and "has ended COMPLETED" in texts[0]
    assert texts[1] == "Say something in words: this Cog reads text only."
    assert all(m["result"]["stopReason"] == "end_turn" for m in messages if m.get("id") in (3, 4))


def test_a_session_on_a_run_that_ended_is_refused(stub, cli, tmp_path):
    run_id = _launched(stub, cli)
    stub.runs[0].update(status="CANCELLED", ended=True)
    client = Client(run_id, tmp_path)
    client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    [refused] = client.close()
    assert refused["error"]["code"] == acp.HUB_FAILED and "has ended CANCELLED" in refused["error"]["message"]


def test_a_cancel_sent_right_behind_its_prompt_is_not_lost(stub, cli, tmp_path):
    run_id = _launched(stub, cli)
    stub.hold_turns = True
    client = Client(run_id, tmp_path)
    client.bridge.answer_timeout = 2  # with the cancel lost, the prompt would wait this long and end_turn
    late_start = client.bridge._prompt

    def scheduled_late(*args):
        threading.Event().wait(0.3)  # the prompt's thread starts after the reader has handled the cancel
        late_start(*args)

    client.bridge._prompt = scheduled_late
    client.send("initialize", {"protocolVersion": 1})
    client.answered(client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []}))
    # Back to back: the reader handles the prompt, then the cancel, before the prompt's thread runs.
    client.send("session/prompt", {"sessionId": f"{run_id}-1", "prompt": [{"type": "text", "text": "slow"}]})
    client.send("session/cancel", {"sessionId": f"{run_id}-1"}, notify=True)
    by_id = {message["id"]: message for message in client.close() if "id" in message}
    assert by_id[3]["result"]["stopReason"] == "cancelled"


def test_a_prompt_with_no_answer_in_time_says_why_and_ends_the_turn(stub, cli, tmp_path):
    run_id = _launched(stub, cli)
    stub.hold_turns = True
    stub.runs[0]["status"] = "SUBMITTED"
    client = Client(run_id, tmp_path)
    client.bridge.answer_timeout = 0.2
    client.send("initialize", {"protocolVersion": 1})
    client.answered(client.send("session/new", {"cwd": str(tmp_path), "mcpServers": []}))
    client.send("session/prompt", {"sessionId": f"{run_id}-1", "prompt": [{"type": "text", "text": "hello"}]})
    messages = client.close()
    [said] = [m["params"]["update"]["content"]["text"] for m in messages if m.get("method") == "session/update"]
    assert said.startswith("No answer within 0.2 seconds: the run is SUBMITTED, and no run controller")
    assert {m["id"]: m for m in messages if "id" in m}[3]["result"]["stopReason"] == "end_turn"


def test_run_say_gives_up_after_its_timeout_and_says_what_the_run_is_doing(stub, cli):
    run_id = _launched(stub, cli)
    stub.hold_turns = True
    stub.runs[0]["status"] = "SUBMITTED"
    said = cli("--hub", HUB, "run", "say", run_id, "hello", "--timeout", "1")
    assert said.exit_code == 1
    assert "no answer within 1 seconds; the run is SUBMITTED (is a run controller watching the hub?)" in said.stderr
