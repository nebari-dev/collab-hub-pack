"""Agent locations: chosen by configuration, never imported; and what the ``local`` location promises.

The lifecycle every location shares is ``test_location_conformance.py``. Here:
the switch, the boundary around it, the directory package source, the run
token, and the isolation of a local worker from its controller.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from locations_support import DEV_COGS, gone, local_executor, packages

from collab_hub_execution import (
    AGENT_LOCATIONS,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    LocationNotImplemented,
    OpDefinition,
    OpStep,
    RunState,
    run_tokens,
)
from collab_hub_execution.locations import select_executor
from collab_hub_execution.locations.packages import DirectoryPackageSource, PackageNotFound, PackageRefused

SOURCE = Path(__file__).resolve().parents[1] / "src" / "collab_hub_execution"

# --- the switch -------------------------------------------------------------------------------------


def test_the_location_setting_takes_two_values_and_local_is_built(tmp_path):
    assert AGENT_LOCATIONS == ("local", "remote")
    runner = LifecycleRunner(track=InMemoryTrackStore(), location="local",
                             location_settings={"packages": [tmp_path], "work_dir": tmp_path / "runs"})
    assert runner.executor.location == "local"


def test_a_location_not_built_yet_is_refused_when_the_runner_starts():
    with pytest.raises(LocationNotImplemented, match="Phase 20"):
        LifecycleRunner(track=InMemoryTrackStore(), location="remote")


def test_an_unknown_location_is_refused():
    with pytest.raises(ValueError, match="unknown agent location 'edge'"):
        LifecycleRunner(track=InMemoryTrackStore(), location="edge")


def test_a_runner_takes_a_location_or_an_executor_and_not_both(tmp_path):
    with pytest.raises(ValueError, match="a location"):
        LifecycleRunner(track=InMemoryTrackStore())
    with pytest.raises(ValueError, match="not both"):
        LifecycleRunner(track=InMemoryTrackStore(), executor=InMemoryCogExecutor({}), location="local")


def _executor_imports(path: Path) -> list[str]:
    """Every import of a concrete location's executor in one file."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:  # syntax newer than this package's floor: the same rule, read line by line
        return [line.strip() for line in path.read_text().splitlines()
                if line.lstrip().startswith(("from ", "import ")) and ("locations.local" in line
                                                                       or "LocalProcessCogExecutor" in line)]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if f".{node.module}".endswith(".locations.local") or node.module == "local" and node.level:
                found.append(f"from {node.module}")
            if any(alias.name == "LocalProcessCogExecutor" for alias in node.names):
                found.append("imports LocalProcessCogExecutor")
        if isinstance(node, ast.Import):
            found += [f"import {alias.name}" for alias in node.names if alias.name.endswith("locations.local")]
    return found


def test_no_caller_imports_a_locations_executor(tmp_path):
    # `location` is the only switch: nothing outside locations/ reaches the local executor.
    repository = SOURCE.parents[2]
    roots = [SOURCE, repository / "scripts", repository / "api" / "src", repository / "dev"]
    assert all(root.is_dir() for root in roots), roots  # a wrong path would scan nothing and pass
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            if SOURCE / "locations" in path.parents or ".pixi" in path.parts:
                continue
            offenders += [f"{path}: {found}" for found in _executor_imports(path)]
    assert offenders == []
    for body, caught in (("from collab_hub_execution.locations.local import LocalProcessCogExecutor\n", True),
                         ("from .locations.local import WorkerStartFailed\n", True),
                         ("import collab_hub_execution.locations.local\n", True),
                         ("from collab_hub_execution.locations import select_executor\n", False)):
        (tmp_path / "caller.py").write_text(body)
        assert bool(_executor_imports(tmp_path / "caller.py")) is caught, body


def test_an_executor_holds_no_lifecycle_logic():
    # An executor brings a worker up and tears it down. It reads no Track, moves no machine, and
    # knows nothing of the runner: what runs next is never decided here.
    lifecycle = {"runner", "track", "states", "gates", "lifecycle", "backends", "orchestration"}
    for path in (SOURCE / "locations").glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {node.module.split(".")[-1] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module}
        imported |= {alias.name.split(".")[-1] for node in ast.walk(tree) if isinstance(node, ast.Import)
                     for alias in node.names}
        assert not imported & lifecycle, (path.name, imported & lifecycle)


# --- the directory package source -------------------------------------------------------------------


def test_the_source_resolves_an_allowlisted_package_and_names_it_by_digest(tmp_path):
    root = packages(tmp_path)
    source = DirectoryPackageSource([root], allow=["echo", "slow"])
    assert source.names() == ("echo", "slow")
    echo = source.resolve("echo")
    assert echo.directory == (root / "echo").resolve() and echo.serve.endswith("serve.py")
    assert echo.digest.startswith("sha256:") and len(echo.digest) == 71
    (root / "echo" / "pixi.lock").write_text("changed")
    assert source.resolve("echo").digest != echo.digest  # the lock is part of what the package is


@pytest.mark.parametrize("name", ["../echo", "..", "echo/../../etc", "/etc", "", "a/b/c", "echo/", "."])
def test_the_source_refuses_a_name_that_is_not_a_package_name(tmp_path, name):
    with pytest.raises(PackageRefused, match="not a package name"):
        DirectoryPackageSource([packages(tmp_path)]).resolve(name)


def test_the_source_refuses_what_is_not_allowlisted_or_leaves_its_root(tmp_path):
    root = packages(tmp_path)
    with pytest.raises(PackageRefused, match="not allowlisted"):
        DirectoryPackageSource([root], allow=["echo"]).resolve("slow")
    outside = tmp_path / "outside"
    shutil.copytree(root / "echo", outside)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackageRefused, match="outside its root"):
        DirectoryPackageSource([root]).resolve("linked")


def test_the_source_finds_no_package_without_a_serve_task(tmp_path):
    root = packages(tmp_path)
    with pytest.raises(PackageNotFound, match="no package 'absent'"):
        DirectoryPackageSource([root]).resolve("absent")
    (root / "echo" / "pixi.toml").write_text('[workspace]\nname = "echo"\n')
    with pytest.raises(PackageNotFound, match="declares no `serve` task"):
        DirectoryPackageSource([root]).resolve("echo")


# --- the run token ----------------------------------------------------------------------------------


def test_a_run_token_verifies_only_for_its_run_and_only_while_its_worker_is_up(tmp_path):
    executor = local_executor(tmp_path)
    track = InMemoryTrackStore()
    seen = {}
    real_teardown = executor.teardown

    def teardown(worker):
        # While the worker is still up: its token verifies against its own run's Track, and no other's.
        seen["token"] = worker._run_token
        seen["up"] = run_tokens.verify(track.replay("r"), worker._run_token)
        seen["forged"] = run_tokens.verify(track.replay("r"), run_tokens.mint())
        seen["other"] = run_tokens.verify(track.replay("another"), worker._run_token)
        real_teardown(worker)

    executor.teardown = teardown
    runner = LifecycleRunner(executor=executor, track=track)
    runner.submit(OpDefinition("another", (OpStep("s", "echo", "run"),)))
    assert runner.submit(OpDefinition("r", (OpStep("s", "echo", "run"),))) is RunState.COMPLETED
    assert seen["up"]["instance"] == "s:0" and seen["up"]["location"] == "local"
    assert seen["forged"] is None and seen["other"] is None
    assert run_tokens.verify(track.replay("r"), seen["token"]) is None  # torn down: the token expired
    # The Track holds the token's hash and where the worker ran, and never the token.
    recorded = json.dumps([event.payload for event in track.replay("r")])
    assert seen["token"] not in recorded and run_tokens.digest(seen["token"]) in recorded
    [started] = [event.payload for event in track.replay("r") if event.event_type == "worker_started"]
    assert set(started) == {"step", "attempt", "instance", "location", "package", "pid", "pgid", "logs",
                            "run_token_sha256"}
    assert started["package"]["name"] == "echo" and started["package"]["digest"].startswith("sha256:")


def test_a_token_expires_with_its_run_even_when_no_stop_was_recorded():
    class Fact:
        def __init__(self, event_type, payload=None):
            self.event_type, self.payload = event_type, payload or {}

    token = run_tokens.mint()
    up = [Fact("worker_started", {"instance": "s:0", "run_token_sha256": run_tokens.digest(token)})]
    assert run_tokens.verify(up, token) is not None
    for end in ("interrupted", "cancelled", "failed", "completed", "budget_exceeded"):
        assert run_tokens.verify([*up, Fact(end)], token) is None, end
    assert run_tokens.verify([*up, Fact("worker_stopped", {"instance": "other:0"})], token) is not None


def test_a_worker_answers_invoke_only_to_the_bearer_of_its_run_token(tmp_path):
    executor = local_executor(tmp_path)
    worker = executor.materialize("echo", "r", "s:0")
    try:
        request = {"entry_point": "run", "input": 1, "idempotency_key": "k"}
        assert httpx.post(f"{worker.url}/invoke", json=request).status_code == 401
        wrong = {"Authorization": f"Bearer {run_tokens.mint()}"}
        assert httpx.post(f"{worker.url}/invoke", json=request, headers=wrong).status_code == 401
        assert worker.interact("run", 1).payload == {"echo": 1}  # the controller presents it
    finally:
        executor.teardown(worker)


# --- what a local worker is given, and what it leaves ------------------------------------------------


def test_a_worker_inherits_nothing_from_its_controller_but_what_it_is_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__DATABASE_URL", "postgresql://controller-only")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "controller-only")
    executor = local_executor(tmp_path, extra_environment={"MODEL_API_KEY": "delivered-by-the-binding"})
    track = InMemoryTrackStore()
    worker = executor.materialize("env", "r", "s:0")
    try:
        env = worker.interact("run").payload["env"]
    finally:
        executor.teardown(worker)
    assert "COLLAB_HUB_API__DATABASE_URL" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "controller-only" not in json.dumps(env)
    # The seam's variables: where to listen, which Cog and run it is, and its run token.
    assert env["COLLAB_COG_HOST"] == "127.0.0.1" and worker.url == f"http://127.0.0.1:{env['COLLAB_COG_PORT']}"
    assert env["COLLAB_COG_ID"] == "env" and env["COLLAB_RUN_ID"] == "r"
    assert run_tokens.digest(env["COLLAB_RUN_TOKEN"]) == worker.run_token_digest
    # What the binding delivers reaches the child's environment, and nothing else of the run.
    assert env["MODEL_API_KEY"] == "delivered-by-the-binding"
    assert LifecycleRunner(executor=executor, track=track).submit(
        OpDefinition("r2", (OpStep("s", "echo", "run"),))) is RunState.COMPLETED
    assert "delivered-by-the-binding" not in json.dumps([event.payload for event in track.replay("r2")])
    written = b"".join(path.read_bytes() for path in (tmp_path / "runs").rglob("*") if path.is_file())
    assert b"delivered-by-the-binding" not in written and env["COLLAB_RUN_TOKEN"].encode() not in written


def test_a_workers_output_goes_to_a_directory_of_its_run(tmp_path):
    executor = local_executor(tmp_path)
    worker = executor.materialize("echo", "team/run 1", "s:0")
    executor.teardown(worker)
    logs = Path(worker.details["logs"])
    assert logs == tmp_path / "runs" / "team_run_1" / "s_0"  # one path segment each: no id leaves work_dir
    assert "serving on 127.0.0.1" in (logs / "stdout.log").read_text()


def test_a_worker_that_exits_before_it_is_ready_fails_the_step_and_says_why(tmp_path):
    root = packages(tmp_path)
    (root / "echo" / "serve.py").write_text("import sys\nsys.exit('the Cog could not start')\n")
    executor = select_executor("local", packages=[root], work_dir=tmp_path / "runs", environment="host")
    track = InMemoryTrackStore()
    state = LifecycleRunner(executor=executor, track=track).submit(OpDefinition("r", (OpStep("s", "echo", "run"),)))
    assert state is RunState.FAILED
    [failed] = [event.payload for event in track.replay("r") if event.event_type == "failed"]
    assert failed["error"] == "WorkerStartFailed" and "the Cog could not start" in failed["reason"]


def test_a_worker_that_is_never_ready_is_killed_and_fails_the_step(tmp_path):
    root = packages(tmp_path)
    (root / "echo" / "serve.py").write_text(
        "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(600)\n")
    executor = select_executor("local", packages=[root], work_dir=tmp_path / "runs", environment="host",
                               ready_timeout=1.0, grace=1.0)
    with pytest.raises(Exception, match="was not ready within 1 seconds"):
        executor.materialize("echo", "r", "s:0")
    assert gone(int((tmp_path / "runs" / "r" / "s_0" / "stdout.log").read_text()))


def test_a_command_that_cannot_be_started_fails_the_step(tmp_path):
    root = packages(tmp_path)
    (root / "echo" / "pixi.toml").write_text('[tasks]\nserve = "no-such-program-here serve.py"\n')
    executor = select_executor("local", packages=[root], work_dir=tmp_path / "runs", environment="host")
    with pytest.raises(Exception, match="could not be started: FileNotFoundError"):
        executor.materialize("echo", "r", "s:0")


def test_teardown_kills_the_workers_whole_process_tree(tmp_path):
    root = packages(tmp_path)
    (root / "echo" / "serve.py").write_text(
        "import subprocess, sys, os\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print(child.pid, flush=True)\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from fake_worker import envelope, serve\n"
        "serve(lambda entry, value, **feedback: envelope(value))\n")
    executor = select_executor("local", packages=[root], work_dir=tmp_path / "runs", environment="host", grace=1.0)
    worker = executor.materialize("echo", "r", "s:0")
    grandchild = int((Path(worker.details["logs"]) / "stdout.log").read_text().split()[0])
    assert not gone(grandchild, within=0.2)
    executor.teardown(worker)
    assert gone(worker.details["pid"]) and gone(grandchild)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux's")
def test_on_linux_a_killed_launcher_takes_its_worker_with_it(tmp_path):
    executor = local_executor(tmp_path)
    worker = executor.materialize("slow", "r", "s:0")
    try:
        os.kill(worker.launcher.pid, signal.SIGKILL)  # the launcher never gets to kill the group
        assert gone(worker.details["pid"])
    finally:
        executor.teardown(worker)


@pytest.mark.skipif(shutil.which("pixi") is None, reason="pixi is not on PATH")
def test_a_package_runs_in_its_own_pixi_environment(tmp_path):
    executor = select_executor("local", packages=[DEV_COGS], allow=["echo"], work_dir=tmp_path / "runs")
    worker = executor.materialize("echo", "r", "s:0")
    try:
        assert worker.interact("run", "hi").payload == {"echo": "hi"}
        # The worker's interpreter is the package environment's, not the controller's.
        ps = subprocess.run(["ps", "-o", "command=", "-g", str(worker.details["pgid"])], capture_output=True,
                            text=True, check=False).stdout
        assert str(DEV_COGS / "echo" / ".pixi") in ps and sys.executable not in ps
    finally:
        executor.teardown(worker)
    assert gone(worker.details["pid"])


def test_without_pixi_the_local_location_says_what_is_missing(tmp_path):
    executor = select_executor("local", packages=[packages(tmp_path)], work_dir=tmp_path / "runs",
                               pixi="no-such-pixi-binary")
    with pytest.raises(Exception, match="pixi is not on PATH"):
        executor.materialize("echo", "r", "s:0")
