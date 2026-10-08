"""Launching a Cog and the runs that makes: `cog launch`, `run list`, `run show`, `run watch`, `run terminate`."""

from __future__ import annotations

import json

import pytest

from collab_hub_cli import main

from .conftest import HUB


@pytest.fixture(autouse=True)
def dev_hub(stub, monkeypatch):
    stub.dev_auth = True
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    return stub


def test_cog_launch_submits_a_one_step_op_and_prints_the_run(stub, cli):
    result = cli("--hub", HUB, "cog", "launch", "echo", "--input", '{"text": "hi"}')
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "run-000000000001"
    assert "on the none backend, workers local" in result.stderr  # so nobody assumes more of either
    [request] = [r for r in stub.requests if r.url.path == "/v1/runs"]
    assert json.loads(request.content) == {"steps": [{
        "name": "echo", "cog": "echo", "entry_point": "run", "input": {"text": "hi"}, "gate": {"escalate": "error"}}]}


def test_cog_launch_takes_the_entry_point_the_gate_and_input_from_stdin(stub, cli):
    stub.launchable = ("acme/echo",)
    result = cli("--hub", HUB, "cog", "launch", "acme/echo", "--entry", "ask", "--gate", "never", "--input", "-",
                 "--json", input='["a", "b"]')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "SUBMITTED"
    [request] = [r for r in stub.requests if r.url.path == "/v1/runs"]
    [step] = json.loads(request.content)["steps"]
    assert step == {"name": "echo", "cog": "acme/echo", "entry_point": "ask", "input": ["a", "b"],
                    "gate": {"escalate": "never"}}


def test_cog_launch_names_the_run_and_run_list_shows_the_name(stub, cli):
    launched = cli("--hub", HUB, "cog", "launch", "echo", "--name", "echo-on-claude")
    assert launched.exit_code == 0 and "run-000000000001 (echo-on-claude)" in launched.stderr
    assert json.loads(stub.requests[-1].content)["name"] == "echo-on-claude"
    lines = cli("--hub", HUB, "run", "list").stdout.splitlines()
    assert lines[1].split()[:3] == ["run-000000000001", "echo-on-claude", "echo"]
    named = cli("--hub", HUB, "--profile", "work", "--insecure", "run", "list").stdout.splitlines()
    assert named[1].endswith(f" --hub {HUB} --profile work --insecure run connect run-000000000001")
    shown = cli("--hub", HUB, "run", "show", "run-000000000001").stdout
    assert "run        run-000000000001  (echo-on-claude)" in shown


def test_a_refused_field_is_named_in_the_error(stub, cli, monkeypatch):
    import httpx

    from collab_hub_cli import hub

    refusal = httpx.Response(422, json={"error": {"code": "validation_error", "message": "Request validation failed",
                                                  "details": [{"loc": ["body", "name"],
                                                               "msg": "String should match pattern"}]}})
    assert hub.error_message(refusal) == ("Request validation failed: name: String should match pattern (HTTP 422)")
    bare = httpx.Response(422, json={"error": {"code": "x", "message": "Refused", "details": {"refused": {}}}})
    assert hub.error_message(bare) == "Refused (HTTP 422)"


def test_cog_launch_without_input_sends_none(stub, cli):
    assert cli("--hub", HUB, "cog", "launch", "echo").exit_code == 0
    assert stub.runs[0]["_input"] is None


@pytest.mark.parametrize(("args", "message"), [
    (("--input", "{not json"), "--input is not JSON"),
    (("--gate", "sometimes"), "--gate is one of never, error, warn, always"),
])
def test_cog_launch_refuses_bad_options_before_calling_the_hub(stub, cli, args, message):
    result = cli("--hub", HUB, "cog", "launch", "echo", *args)
    assert result.exit_code == 2 and message in result.stderr
    assert not [r for r in stub.requests if r.url.path == "/v1/runs"]


def test_a_cog_the_hub_cannot_launch_is_reported_with_those_it_can(stub, cli):
    result = cli("--hub", HUB, "cog", "launch", "hermes")
    assert result.exit_code == 1
    assert "Cannot launch hermes; the Cogs this hub launches are: echo, slow" in result.stderr


@pytest.mark.parametrize(("ends", "code"), [("COMPLETED", 0), ("FAILED", 1), ("INTERRUPTED", 3),
                                            ("WAITING_AT_GATE", 4), ("CANCELLED", 6), ("BUDGET_EXCEEDED", 1)])
def test_watch_follows_the_run_and_exits_with_its_outcome(stub, cli, ends, code):
    stub.progress["run-000000000001"] = ["RUNNING", "RUNNING", ends]
    result = cli("--hub", HUB, "cog", "launch", "echo", "--watch")
    assert result.exit_code == code, result.output
    assert result.stderr.count("run-000000000001: RUNNING") == 1  # each status is said once
    assert f"run-000000000001: {ends}" in result.stderr
    assert f"status     {ends}" in result.stdout and "echo" in result.stdout
    assert ("echo answered:" in result.stdout and '"greeting": "hi"' in result.stdout) is (ends == "COMPLETED")
    watched = cli("--hub", HUB, "run", "watch", "run-000000000001", "--json")
    assert watched.exit_code == code and json.loads(watched.stdout)["status"] == ends


def test_run_list_shows_what_was_launched_and_filters_by_status(stub, cli):
    assert cli("--hub", HUB, "run", "list").stderr.strip() == "No runs."
    assert json.loads(cli("--hub", HUB, "run", "list", "--json").stdout) == []
    cli("--hub", HUB, "cog", "launch", "echo")
    cli("--hub", HUB, "cog", "launch", "slow")
    stub.runs[1].update(status="COMPLETED", ended=True)
    lines = cli("--hub", HUB, "run", "list").stdout.splitlines()
    assert lines[0].split() == ["RUN", "NAME", "COG", "STATUS", "AGE", "BY", "CONNECT"]
    assert [line.split()[:3] for line in lines[1:]] == [["run-000000000002", "slow", "SUBMITTED"],
                                                        ["run-000000000001", "echo", "COMPLETED"]]
    # What an ACP client starts to talk to a run that has not ended; an ended one takes no more turns.
    assert lines[1].endswith(f" --hub {HUB} run connect run-000000000002")
    assert "run connect" not in lines[2]
    only = json.loads(cli("--hub", HUB, "run", "list", "--status", "completed", "--json").stdout)
    assert [run["id"] for run in only] == ["run-000000000001"]


def test_run_show_prints_the_run_its_error_and_a_missing_run_is_the_hubs_404(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.runs[0].update(status="FAILED", ended=True, error="model-call-failed", reason="no")
    stub.runs[0]["steps"][0].update(state="failed", error="model-call-failed")
    shown = cli("--hub", HUB, "run", "show", "run-000000000001")
    assert shown.exit_code == 0, shown.output
    assert "error      model-call-failed: no" in shown.stdout and "backend none, workers local" in shown.stdout
    assert json.loads(cli("--hub", HUB, "run", "show", "run-000000000001", "--json").stdout)["status"] == "FAILED"
    missing = cli("--hub", HUB, "run", "show", "run-nope")
    assert missing.exit_code == 1 and "No run run-nope (HTTP 404)" in missing.stderr


def test_run_terminate_asks_for_the_cancel_and_waits_until_the_run_has_ended(stub, cli):
    cli("--hub", HUB, "cog", "launch", "slow")
    stub.runs[0]["status"] = "RUNNING"
    stub.progress["run-000000000001"] = ["RUNNING", "CANCELLED"]
    result = cli("--hub", HUB, "run", "terminate", "run-000000000001")
    assert result.exit_code == 0, result.output
    assert "run-000000000001 ended CANCELLED." in result.stderr and result.stdout == ""
    assert stub.runs[0]["cancel_requested_by"] == "dev-user"
    again = cli("--hub", HUB, "run", "terminate", "run-000000000001")
    assert again.exit_code == 1 and "cannot be cancelled: the run has ended CANCELLED (HTTP 409)" in again.stderr


def test_run_terminate_no_wait_returns_once_the_request_is_recorded(stub, cli):
    cli("--hub", HUB, "cog", "launch", "slow")
    stub.runs[0]["status"] = "RUNNING"
    result = cli("--hub", HUB, "run", "terminate", "run-000000000001", "--no-wait")
    assert result.exit_code == 0 and result.stdout.strip() == "RUNNING"
    assert "cancel requested; it is still RUNNING" in result.stderr
    assert json.loads(cli("--hub", HUB, "run", "terminate", "run-000000000001", "--no-wait", "--json").stdout)[
        "cancel_requested_by"] == "dev-user"


def test_a_run_that_ended_on_its_own_before_the_cancel_landed_says_so(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000001"] = ["COMPLETED"]
    result = cli("--hub", HUB, "run", "terminate", "run-000000000001")
    assert result.exit_code == 1 and "run-000000000001 ended COMPLETED." in result.stderr


def test_the_run_commands_need_a_session_on_a_hub_that_asks_for_one(stub, cli):
    stub.dev_auth = False
    for args in (("cog", "launch", "echo"), ("run", "list"), ("run", "show", "x"), ("run", "terminate", "x")):
        result = cli("--hub", HUB, *args)
        assert result.exit_code == 5 and "not signed in" in result.stderr, args


def test_the_connect_command_runs_from_any_shell(stub, cli, tmp_path, monkeypatch):
    import shlex

    from collab_hub_cli import main

    cli("--hub", HUB, "cog", "launch", "echo")
    program = tmp_path / "bin" / "collab-hub"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setattr(main.sys, "argv", [str(program)])
    # The client is given the program's path, even where this shell finds it by name, and the configuration
    # directory it signed in with.
    monkeypatch.setenv("PATH", str(program.parent))
    line = cli("--hub", HUB, "run", "list").stdout.splitlines()[1]
    command = shlex.split(line[line.index("env "):])
    assert command == ["env", f"COLLAB_HUB_CONFIG_DIR={tmp_path / 'config'}", str(program), "--hub", HUB,
                       "run", "connect", "run-000000000001"]
    # In the default configuration directory, the command needs none.
    monkeypatch.delenv("COLLAB_HUB_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    line = cli("--hub", HUB, "run", "list").stdout.splitlines()[1]
    assert line.endswith(f"  {program} --hub {HUB} run connect run-000000000001")
