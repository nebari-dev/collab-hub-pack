"""The location conformance suite: what every agent location does the same way.

A worker is materialized, becomes ready, answers ``/invoke`` with an envelope,
is torn down, and is cancelled mid-interaction, identically wherever it runs,
because the lifecycle runner drives every location through the same executor
interface and the same seam. The suite runs against ``local``, a real process
per worker, and against the in-memory executor; ``remote`` joins it with Phase
21. What only a location with processes can show, that a controller killed with
``SIGKILL`` leaves no worker, runs where there are processes.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from locations_support import gone, in_memory_executor, local_executor, packages

from collab_hub_execution import InMemoryTrackStore, LifecycleRunner, OpDefinition, OpStep, RunState

LOCATIONS = ("local", "in-memory")
_WORKER_FACTS = {"worker_started", "worker_stopped"}


@pytest.fixture(params=LOCATIONS)
def executor(request, tmp_path):
    return local_executor(tmp_path) if request.param == "local" else in_memory_executor()


def _has_processes(executor) -> bool:
    return getattr(executor, "location", None) == "local"


def _runner(executor, track=None) -> LifecycleRunner:
    return LifecycleRunner(executor=executor, track=track or InMemoryTrackStore())


def _types(track, run_id, *, without=frozenset()):
    return [event.event_type for event in track.replay(run_id) if event.event_type not in without]


def test_a_worker_is_materialized_ready_answers_and_is_torn_down(executor):
    worker = executor.materialize("echo", "run-1", "step:0")  # returns once the worker is ready
    try:
        envelope = worker.interact("run", {"text": "hello"}, idempotency_key="run-1:step:0")
        assert envelope.ok and envelope.payload == {"echo": {"text": "hello"}}
    finally:
        executor.teardown(worker)
    if _has_processes(executor):
        assert gone(worker.details["pid"])
        with pytest.raises(Exception):  # noqa: B017, PT011 - nothing listens any more
            worker.interact("run", {})
        executor.teardown(worker)  # tearing down twice is the same as once


def test_a_send_back_reaches_the_worker_as_its_signal(executor):
    worker = executor.materialize("needs-review", "run-1", "draft:1")
    try:
        first = worker.interact("run", "text")
        again = worker.interact("run", "text", signal=["cite a source"])
    finally:
        executor.teardown(worker)
    assert [problem.severity for problem in first.problems] == ["error"]
    assert again.problems == () and again.payload == {"draft": "text", "revised_for": ["cite a source"]}


def test_an_error_envelope_comes_back_as_one(executor):
    worker = executor.materialize("fails", "run-1", "attempt:0")
    try:
        envelope = worker.interact("run")
    finally:
        executor.teardown(worker)
    assert not envelope.ok and envelope.error.code == "model-call-failed"


def test_two_workers_run_at_once_without_colliding(executor):
    workers, errors = [], []

    def bring_up(instance):
        try:
            workers.append(executor.materialize("echo", "run-1", instance))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=bring_up, args=(f"step:{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert errors == [] and len(workers) == 2
        assert [w.interact("run", n).payload for n, w in enumerate(workers)] == [{"echo": 0}, {"echo": 1}]
        if _has_processes(executor):
            assert workers[0].url != workers[1].url
            assert workers[0].details["pid"] != workers[1].details["pid"]
    finally:
        for worker in workers:
            executor.teardown(worker)


def test_a_run_records_the_same_lifecycle_at_every_location(tmp_path):
    # The Track of one Op, location by location: the same moves in the same order, and beside them,
    # where a worker is a process, where it ran and when it stopped.
    op = OpDefinition("r", (OpStep("first", "echo", "run", {"n": 1}), OpStep("second", "echo", "run", {"n": 2})))
    tracks = {}
    for name, executor in (("local", local_executor(tmp_path)), ("in-memory", in_memory_executor())):
        tracks[name] = InMemoryTrackStore()
        assert _runner(executor, tracks[name]).submit(op) is RunState.COMPLETED
    assert _types(tracks["local"], "r", without=_WORKER_FACTS) == _types(tracks["in-memory"], "r")
    local = _types(tracks["local"], "r")
    assert local.count("worker_started") == local.count("worker_stopped") == 2
    assert not _WORKER_FACTS & set(_types(tracks["in-memory"], "r"))


def test_a_run_cancelled_mid_interaction_ends_cancelled_and_leaves_no_worker(executor):
    processes = _has_processes(executor)
    track = InMemoryTrackStore()
    runner = _runner(executor, track)
    # Where the worker is a process, cancelling kills it, so the step need not finish for the run to end.
    op = OpDefinition("r", (OpStep("wait", "slow", "run", {"seconds": 60 if processes else 1}),
                            OpStep("never", "echo", "run")))
    submitted = threading.Thread(target=runner.submit, args=(op,))
    submitted.start()
    deadline = time.monotonic() + 30
    while "interaction_started" not in _types(track, "r"):
        assert time.monotonic() < deadline, _types(track, "r")
        time.sleep(0.02)
    started = time.monotonic()
    runner.cancel("r", actor="alice")
    submitted.join(timeout=30)
    assert not submitted.is_alive()
    assert runner.observe("r") is RunState.CANCELLED
    events = list(track.replay("r"))
    assert not any(event.payload.get("step") == "never" for event in events)
    if processes:
        assert time.monotonic() - started < 20  # it did not wait out the sixty seconds
        [worker] = [event.payload for event in events if event.event_type == "worker_started"]
        assert gone(worker["pid"])
        assert [event.payload["instance"] for event in events if event.event_type == "worker_stopped"] == ["wait:0"]


_CONTROLLER = textwrap.dedent("""
    import sys, time
    from collab_hub_execution.locations import select_executor

    executor = select_executor("local", packages=[sys.argv[1]], work_dir=sys.argv[2], environment="host")
    worker = executor.materialize("slow", "run-1", "wait:0")
    print(worker.details["pid"], worker.launcher.pid, flush=True)
    time.sleep(600)
""")


@pytest.mark.parametrize("location", LOCATIONS)
def test_a_controller_killed_with_sigkill_leaves_no_worker(location, tmp_path):
    if location != "local":
        pytest.skip("an in-memory worker is not a process: it cannot outlive its controller")
    controller = subprocess.Popen([sys.executable, "-c", _CONTROLLER, str(packages(tmp_path)), str(tmp_path / "runs")],
                                  stdout=subprocess.PIPE, text=True)
    try:
        worker, launcher = (int(pid) for pid in controller.stdout.readline().split())
        assert not gone(worker, within=0.5)  # it is up, and would stay up for as long as its controller
        controller.send_signal(signal.SIGKILL)  # no handler runs, no teardown is called
        controller.wait(timeout=10)
        assert gone(worker), "the worker outlived its controller"
        assert gone(launcher)
    finally:
        controller.kill()
