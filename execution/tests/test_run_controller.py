"""Intent on the Track, and the controller that acts on it (ADR-0002 D4).

The API's half is ``intents``: it records a submission or a request to cancel,
and reads a run back. The controller's half is ``RunController``: it starts
what was submitted and delivers what was asked. Neither calls the other.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest
from locations_support import gone, packages

from collab_hub_execution import (
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OneSubmissionPerRun,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunState,
    SqliteTrackStore,
    intents,
)
from collab_hub_execution.controller import RunController

BY = {"user": "alice", "org_id": "acme"}


def _controller(track, handlers):
    return RunController(LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=track), poll_interval=0.01)


def _settle(controller, until=lambda: True):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        controller.tick()
        if until() and controller.idle():
            return
        time.sleep(0.01)
    raise AssertionError("the controller did not settle")


def _types(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


def test_a_submission_is_only_intent_until_a_controller_picks_it_up():
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("a", "echo", "run", 1), OpStep("b", "echo", "run", 2)))
    intents.submit(track, op, by=BY)
    assert _types(track, "r") == ["op_submitted"]
    view = intents.describe(track, "r")
    assert view.state is RunState.SUBMITTED and view.submitted_by == BY
    assert [(step.name, step.cog, step.state) for step in view.steps] == [("a", "echo", "pending"),
                                                                           ("b", "echo", "pending")]
    controller = _controller(track, {"echo": lambda entry, value: value})
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.COMPLETED and [step.state for step in view.steps] == ["completed", "completed"]
    assert _types(track, "r")[:2] == ["op_submitted", "run_picked_up"]
    _settle(controller)  # an ended run is not picked up again
    assert _types(track, "r").count("run_picked_up") == 1


def test_a_run_id_is_submitted_once():
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("a", "echo", "run"),))
    intents.submit(track, op, by=BY)
    with pytest.raises(OneSubmissionPerRun):
        intents.submit(track, op, by=BY)


def test_a_failed_step_is_told_with_its_error():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "fails", "run"), OpStep("b", "fails", "run"))), by=BY)
    _settle(_controller(track, {"fails": lambda entry, value: ResultEnvelope.failure("model-call-failed", "no")}))
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "model-call-failed" and view.reason == "no"
    assert [(step.state, step.error) for step in view.steps] == [("failed", "model-call-failed"), ("pending", None)]


def test_a_request_to_cancel_is_delivered_to_the_run_in_flight():
    track, release = InMemoryTrackStore(), threading.Event()

    def slow(entry, value):
        release.wait(10)
        return value

    intents.submit(track, OpDefinition("r", (OpStep("wait", "slow", "run"), OpStep("never", "slow", "run"))), by=BY)
    controller = _controller(track, {"slow": slow})
    deadline = time.monotonic() + 10
    while "interaction_started" not in _types(track, "r"):
        assert time.monotonic() < deadline
        controller.tick()
        time.sleep(0.01)
    asked = intents.request_cancel(track, "r", actor="bob")
    assert asked.state is RunState.RUNNING and asked.cancel_requested_by == "bob"
    assert intents.request_cancel(track, "r", actor="carol").cancel_requested_by == "bob"  # recorded once
    controller.tick()
    release.set()
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.CANCELLED
    assert [step.state for step in view.steps] == ["cancelled", "pending"]
    assert [e.payload for e in track.replay("r") if e.event_type == "cancelled"] == [{"actor": "bob"}]
    with pytest.raises(intents.RunEnded, match="has ended CANCELLED"):
        intents.request_cancel(track, "r", actor="bob")


def test_a_run_cancelled_before_pickup_never_starts_and_an_unknown_run_is_not_cancelled():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
    intents.request_cancel(track, "r", actor="bob")
    controller = _controller(track, {"echo": lambda entry, value: value})
    _settle(controller)
    assert intents.describe(track, "r").state is RunState.CANCELLED
    assert controller.runner.executor.materialized == []
    with pytest.raises(LookupError):
        intents.request_cancel(track, "absent", actor="bob")
    assert intents.describe(track, "absent") is None


def test_a_starting_controller_interrupts_what_a_stopped_one_left_and_leaves_submissions_alone():
    track = InMemoryTrackStore()

    class Stops(BaseException):
        pass

    def stops(entry, value):
        raise Stops

    intents.submit(track, OpDefinition("left", (OpStep("a", "c", "run"),)), by=BY)
    with pytest.raises(Stops):
        LifecycleRunner(executor=InMemoryCogExecutor({"c": stops}), track=track).submit(
            OpDefinition("left", (OpStep("a", "c", "run"),)))
    intents.submit(track, OpDefinition("waiting", (OpStep("a", "c", "run"),)), by=BY)
    controller = _controller(track, {"c": lambda entry, value: value})
    assert controller.start() == ("left",)
    _settle(controller)
    assert intents.describe(track, "left").state is RunState.INTERRUPTED
    assert [step.state for step in intents.describe(track, "left").steps] == ["interrupted"]
    assert intents.describe(track, "waiting").state is RunState.COMPLETED
    assert [view.run_id for view in intents.list_runs(track)] == ["waiting", "left"]  # newest first


def test_one_runs_failure_does_not_stop_the_controller():
    track = InMemoryTrackStore()

    class Broken:
        def materialize(self, cog, run_id, instance=""):
            raise KeyboardInterrupt if run_id == "never" else RuntimeError("no worker today")

        def teardown(self, worker):
            pass

    intents.submit(track, OpDefinition("r", (OpStep("a", "c", "run"),)), by=BY)
    controller = RunController(LifecycleRunner(executor=Broken(), track=track), poll_interval=0.01)
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "RuntimeError"


def test_the_controller_process_runs_a_submitted_package_and_holds_its_track(tmp_path):
    # The process itself: a submission written to a SQLite Track is run as a real worker, a second
    # controller on the same Track refuses to start, and stopping it leaves no worker.
    track_path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(track_path)
    track = SqliteTrackStore(track_path)
    command = [sys.executable, "-m", "collab_hub_execution.controller", "--track", str(track_path),
               "--packages", str(packages(tmp_path)), "--work-dir", str(tmp_path / "runs"),
               "--poll-interval", "0.05"]
    # The fake packages run under this interpreter here, so the executor is told not to use pixi.
    controller = subprocess.Popen([*command, "--environment", "host"], stderr=subprocess.PIPE, text=True)
    try:
        intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run", {"n": 1}),)), by=BY)
        deadline = time.monotonic() + 30
        while intents.describe(track, "r").state is not RunState.COMPLETED:
            assert time.monotonic() < deadline and controller.poll() is None, _types(track, "r")
            time.sleep(0.05)
        second = subprocess.run([*command, "--environment", "host"], capture_output=True, text=True, timeout=30)
        assert second.returncode == 1 and "one host at a time" in second.stderr
        intents.submit(track, OpDefinition("slow", (OpStep("a", "slow", "run", {"seconds": 60}),)), by=BY)
        while "interaction_started" not in _types(track, "slow"):
            assert time.monotonic() < deadline and controller.poll() is None
            time.sleep(0.05)
        [worker] = [e.payload["pid"] for e in track.replay("slow") if e.event_type == "worker_started"]
    finally:
        controller.terminate()
        controller.wait(timeout=30)
    assert controller.returncode == 0
    assert gone(worker)
