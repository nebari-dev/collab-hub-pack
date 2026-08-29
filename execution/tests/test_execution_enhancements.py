"""Budget enforcement, bounded revise loops, and per-step digests on the engine.

These complete intents that existed as unused primitives: lifecycle budgets were
defined but never enforced in a run, and the Track lacked per-step digests and a
bounded revise loop.
"""

from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_execution import (
    DurableWorkflowEngine,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    PauseRequest,
    RunBudget,
    RunStatus,
    TrackEvent,
    derive_run_status,
)
from collab_hub_execution.orchestration import _serialize_op


def test_token_budget_survives_restart_and_stops_the_run():
    """Budget is reconstructed from the Track, so it holds across an engine restart."""
    def always_usage(entry, value):
        return {"result": value, "usage": {"tokens": 60}}

    state = {"paused": True}

    def gate_then_usage(entry, value):
        if state["paused"]:
            raise PauseRequest("approve step 2")
        return {"result": value, "usage": {"tokens": 60}}

    track = InMemoryTrackStore()
    budget = RunBudget(max_tokens=100)
    op = OpDefinition("run-budget", (OpStep("s1", "a", "run", "x"), OpStep("s2", "b", "run", "y")))

    e1 = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"a": always_usage, "b": gate_then_usage}),
        track=track,
        budget=budget,
    )
    assert e1.submit(op) is RunStatus.PAUSED  # s1 consumes 60 (<100); s2 pauses

    state["paused"] = False
    e2 = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"a": always_usage, "b": gate_then_usage}),
        track=track,
        budget=budget,
    )
    # fresh engine: reconstructed tracker already holds s1's 60; s2's 60 -> 120 > 100
    assert e2.signal("run-budget", "approved") is RunStatus.BUDGET_EXCEEDED
    assert any(e.event_type == "budget_exceeded" for e in track.replay("run-budget"))


def test_duration_budget_stops_run_before_any_step():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"a": lambda e, v: v}),
        track=track,
        budget=RunBudget(max_duration=timedelta(0)),
    )
    assert engine.submit(OpDefinition("run-dur", (OpStep("s", "a", "run"),))) is RunStatus.BUDGET_EXCEEDED


def test_bounded_revise_loop_fails_after_max_revisions():
    def always_pause(entry, value):
        raise PauseRequest("needs another revision")

    track = InMemoryTrackStore()
    op = OpDefinition("run-revise", (OpStep("draft", "writer", "revise", "v0"),))

    def engine():
        return DurableWorkflowEngine(
            executor=InMemoryCogExecutor({"writer": always_pause}),
            track=track,
            max_revisions=2,
        )

    assert engine().submit(op) is RunStatus.PAUSED             # pause 1
    assert engine().signal("run-revise", "fix a") is RunStatus.PAUSED   # pause 2
    assert engine().signal("run-revise", "fix b") is RunStatus.FAILED   # exceeds max_revisions
    assert any(
        e.event_type == "failed" and e.payload.get("error") == "RevisionLimitExceeded"
        for e in track.replay("run-revise")
    )


def test_step_digest_is_recorded_and_survives_restart():
    state = {"paused": True}

    def handler(entry, value):
        if state["paused"]:
            raise PauseRequest("approve")
        return value

    track = InMemoryTrackStore()
    op = OpDefinition("run-digest", (OpStep("s", "cog", "run", "x", digest="sha256:abc"),))
    DurableWorkflowEngine(executor=InMemoryCogExecutor({"cog": handler}), track=track).submit(op)

    started = [e for e in track.replay("run-digest") if e.event_type == "step_started"]
    assert started and started[0].payload.get("digest") == "sha256:abc"

    # restart: the op is reconstructed from the Track alone; the digest must round-trip
    state["paused"] = False
    restarted = DurableWorkflowEngine(executor=InMemoryCogExecutor({"cog": handler}), track=track)
    assert restarted.signal("run-digest", "ok") is RunStatus.COMPLETED
    materialized = [e for e in track.replay("run-digest") if e.event_type == "materialized"]
    assert materialized and all(e.payload.get("digest") == "sha256:abc" for e in materialized)


# --- failure handling (#5), idempotency key (#3), duplicate step names (#7) ---


class _FailingMaterializeExecutor:
    def materialize(self, cog, run_id):
        raise RuntimeError("api down")

    def teardown(self, worker):  # never called (materialize failed)
        raise AssertionError("teardown should not run when materialize failed")


def test_materialize_failure_is_recorded_durably_not_left_hanging():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=_FailingMaterializeExecutor(), track=track)
    status = engine.submit(OpDefinition("run-mat", (OpStep("s", "c", "run"),)))
    assert status is RunStatus.FAILED
    events = [e.event_type for e in track.replay("run-mat")]
    assert "failed" in events and "completed" not in events  # terminal, not stuck


class _CapturingWorker:
    def __init__(self):
        self.keys = []

    def interact(self, entry_point, input=None, idempotency_key=None):
        self.keys.append(idempotency_key)
        return {"ok": True}


class _CapturingExecutor:
    def __init__(self, worker):
        self.worker = worker
        self.torn = 0

    def materialize(self, cog, run_id):
        return self.worker

    def teardown(self, worker):
        self.torn += 1


def test_engine_passes_a_stable_idempotency_key_and_tears_down_in_finally():
    worker = _CapturingWorker()
    ex = _CapturingExecutor(worker)
    engine = DurableWorkflowEngine(executor=ex, track=InMemoryTrackStore())
    engine.submit(OpDefinition("run-key", (OpStep("draft", "c", "run"),)))
    assert worker.keys == ["run-key:draft:0"]
    assert ex.torn == 1


def test_duplicate_step_names_are_rejected_at_submit():
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=InMemoryTrackStore()
    )
    with pytest.raises(ValueError):
        engine.submit(OpDefinition("run-dup", (OpStep("dup", "c", "run"), OpStep("dup", "c", "run"))))


# --- crash-recovery idempotency (#1) + worker honoring the key (#6) ---


class _SameWorkerExecutor:
    def __init__(self, worker):
        self.worker = worker

    def materialize(self, cog, run_id):
        return self.worker

    def teardown(self, worker):
        pass


class _KeyHonoringWorker:
    """Durable-store stand-in: a replayed key returns the prior result and does
    not repeat the side effect. The first call performs its side effect and then
    the process dies (SystemExit) before the step outcome is recorded — so the run
    is left mid-step (non-terminal), which is what a genuine crash looks like.

    This proves the *engine's* key-stability contract on resume; a real worker
    that does not persist keys across pod replacement is at-least-once (see the
    CogWorker.interact docstring and #1).
    """

    def __init__(self):
        self.side_effects = []
        self._seen = {}
        self._crash_once = True

    def interact(self, entry_point, input=None, idempotency_key=None):
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]  # replay -> no repeated side effect
        self.side_effects.append(idempotency_key)  # the side effect
        self._seen[idempotency_key] = {"done": idempotency_key}
        if self._crash_once:
            self._crash_once = False
            raise SystemExit("process died after side effect, before completion")
        return self._seen[idempotency_key]


def test_crash_recovery_resumes_with_the_same_key_so_the_side_effect_runs_once():
    worker = _KeyHonoringWorker()
    engine = DurableWorkflowEngine(executor=_SameWorkerExecutor(worker), track=InMemoryTrackStore())
    op = OpDefinition("run-idem", (OpStep("s", "c", "run"),))
    with pytest.raises(SystemExit):  # process dies mid-step; run left non-terminal
        engine.submit(op)
    assert engine.observe("run-idem") not in (RunStatus.COMPLETED, RunStatus.FAILED)
    assert engine.submit(op) is RunStatus.COMPLETED  # resume re-drives with the SAME key
    assert worker.side_effects == ["run-idem:s:0"]  # performed exactly once


# --- terminal runs are immutable; re-running takes an explicit retry() (#3) ---


def test_re_submitting_a_failed_run_is_a_no_op_not_a_silent_re_execution():
    calls = {"n": 0}

    def fail(entry, value):
        calls["n"] += 1
        raise RuntimeError("boom")

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": fail}), track=track)
    op = OpDefinition("run-term", (OpStep("s", "c", "run"),))
    assert engine.submit(op) is RunStatus.FAILED
    before = len(track.replay("run-term"))
    assert engine.submit(op) is RunStatus.FAILED  # immutable: no re-drive
    assert calls["n"] == 1                          # the step did NOT run again
    assert len(track.replay("run-term")) == before  # no new events appended


class _RetryProbeExecutor:
    """Records the idempotency key of each interaction; first attempt fails."""

    def __init__(self):
        self.keys = []
        self.attempts = 0

    class _Worker:
        cog = "c"

        def __init__(self, outer):
            self._outer = outer

        def interact(self, entry_point, input=None, idempotency_key=None):
            self._outer.keys.append(idempotency_key)
            self._outer.attempts += 1
            if self._outer.attempts == 1:
                raise RuntimeError("first attempt fails")
            return {"ok": True}

    def materialize(self, cog, run_id):
        return self._Worker(self)

    def teardown(self, worker):
        pass


def test_retry_re_drives_a_failed_run_under_a_fresh_key():
    ex = _RetryProbeExecutor()
    engine = DurableWorkflowEngine(executor=ex, track=InMemoryTrackStore())
    op = OpDefinition("run-retry", (OpStep("s", "c", "run"),))
    assert engine.submit(op) is RunStatus.FAILED
    assert engine.retry("run-retry") is RunStatus.COMPLETED
    # explicit retry is a new attempt -> fresh key, so the work genuinely re-runs
    assert ex.keys == ["run-retry:s:0", "run-retry:s:1"]


def test_retry_rejects_a_non_terminal_run():
    def gate(entry, value):
        raise PauseRequest("hold")

    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": gate}), track=InMemoryTrackStore())
    assert engine.submit(OpDefinition("run-paused", (OpStep("s", "c", "run"),))) is RunStatus.PAUSED
    with pytest.raises(ValueError):
        engine.retry("run-paused")


def test_retry_rejects_a_completed_run():
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=InMemoryTrackStore()
    )
    assert engine.submit(OpDefinition("run-ok", (OpStep("s", "c", "run"),))) is RunStatus.COMPLETED
    with pytest.raises(ValueError):  # nothing to retry; re-running finished work is a new Op
        engine.retry("run-ok")


# --- a paused run resumes only through signal(), never a re-submit ---


def test_re_submitting_a_paused_run_does_not_resume_it_behind_the_gate():
    calls = {"n": 0}
    state = {"approved": False}

    def gate(entry, value):
        calls["n"] += 1
        if not state["approved"]:
            raise PauseRequest("approve")
        return value

    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": gate}), track=track)

    op = OpDefinition("run-gate", (OpStep("s", "c", "run", "x"),))
    assert engine().submit(op) is RunStatus.PAUSED
    assert calls["n"] == 1
    # a re-submit must NOT re-invoke the gated step (that would bypass the gate)
    assert engine().submit(op) is RunStatus.PAUSED
    assert calls["n"] == 1
    # the gate opens only through signal(), which carries the decision value
    state["approved"] = True
    assert engine().signal("run-gate", "go") is RunStatus.COMPLETED


# --- duration budget must survive a crash between the two submission events ---


def test_duration_budget_survives_a_crash_between_the_two_submission_events():
    track = InMemoryTrackStore()
    op = OpDefinition("run-crash-budget", (OpStep("s", "c", "run"),))
    # Simulate a crash after op_submitted committed but before `submitted`: only the
    # first event exists, and it happened long ago.
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    track.append(
        TrackEvent(
            run_id="run-crash-budget",
            event_type="op_submitted",
            payload={"op": _serialize_op(op)},
            occurred_at=long_ago,
        )
    )
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: v}),
        track=track,
        budget=RunBudget(max_duration=timedelta(minutes=5)),
    )
    # recovery anchors elapsed time to op_submitted (an hour ago), not now, so the
    # 5-minute duration budget is correctly seen as exceeded
    assert engine.submit(op) is RunStatus.BUDGET_EXCEEDED


# --- signal values are durable and recovered from the Track ---


def test_signal_value_is_durable_across_a_crash_mid_resume():
    approval = {"approved": True}
    seen = []
    crash = {"once": True}

    def handler(entry, value):
        if value != approval:  # the original input pauses at the gate
            raise PauseRequest("approve")
        seen.append(value)  # resumed with the durable signal
        if crash["once"]:
            crash["once"] = False
            raise SystemExit("crash after signal consumed, before completion")
        return {"ok": True}

    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": handler}), track=track)

    op = OpDefinition("run-sig", (OpStep("s", "c", "review", "draft-v1"),))
    assert engine().submit(op) is RunStatus.PAUSED
    with pytest.raises(SystemExit):  # crash while resuming with the approval
        engine().signal("run-sig", approval)
    # recovery must replay with the approval from the Track, never the original input
    assert engine().submit(op) is RunStatus.COMPLETED
    assert seen == [approval, approval]
    assert "draft-v1" not in seen


def test_signal_can_resume_a_step_with_an_explicit_none():
    seen = []

    def handler(entry, value):
        if value == "GATE":  # original input pauses; a signal (even None) resumes
            raise PauseRequest("approve")
        seen.append(value)
        return {"ok": True}

    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": handler}), track=track)

    op = OpDefinition("run-none", (OpStep("s", "c", "run", "GATE"),))
    assert engine().submit(op) is RunStatus.PAUSED
    assert engine().signal("run-none", None) is RunStatus.COMPLETED
    assert seen == [None]  # the step saw the signal None, not its original input "GATE"


# --- idempotency keys are injective even when ids contain the delimiter ---


class _KeyCapture:
    class _Worker:
        cog = "c"

        def __init__(self, outer):
            self._outer = outer

        def interact(self, entry_point, input=None, idempotency_key=None):
            self._outer.keys.append(idempotency_key)
            return {"ok": True}

    def __init__(self):
        self.keys = []

    def materialize(self, cog, run_id):
        return self._Worker(self)

    def teardown(self, worker):
        pass


def test_idempotency_keys_are_injective_across_ids_containing_the_delimiter():
    cap1 = _KeyCapture()
    DurableWorkflowEngine(executor=cap1, track=InMemoryTrackStore()).submit(
        OpDefinition("a:b", (OpStep("c", "c", "run"),))
    )
    cap2 = _KeyCapture()
    DurableWorkflowEngine(executor=cap2, track=InMemoryTrackStore()).submit(
        OpDefinition("a", (OpStep("b:c", "c", "run"),))
    )
    # ("a:b", "c") and ("a", "b:c") must not collapse to the same key
    assert cap1.keys and cap2.keys
    assert cap1.keys[0] != cap2.keys[0]


# --- a mid-run poll reads RUNNING, not the terminal-sounding TEARING_DOWN ---


def test_between_steps_status_is_running_not_tearing_down():
    per_step = ["step_started", "materialized", "ready", "interaction_started", "idle", "teardown_started"]
    events = [TrackEvent(run_id="r", event_type=t) for t in [*per_step, "step_completed"]]
    assert derive_run_status(events) is RunStatus.RUNNING  # between steps, still progressing
    events.append(TrackEvent(run_id="r", event_type="completed"))
    assert derive_run_status(events) is RunStatus.COMPLETED  # the final event still wins


# --- budget limits are inclusive: reaching exactly the max stops the run ---


def test_budget_boundary_is_inclusive_so_exact_max_is_exceeded():
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: {"result": v, "usage": {"tokens": 60}}}),
        track=InMemoryTrackStore(),
        budget=RunBudget(max_tokens=60),
    )
    assert engine.submit(OpDefinition("run-exact", (OpStep("s", "c", "run"),))) is RunStatus.BUDGET_EXCEEDED


class _TeardownFailsExecutor:
    class _Worker:
        cog = "c"

        def interact(self, entry_point, input=None, idempotency_key=None):
            return {"ok": True}

    def materialize(self, cog, run_id):
        return self._Worker()

    def teardown(self, worker):
        raise RuntimeError("delete failed")


def test_teardown_failure_fails_the_run_rather_than_reporting_success():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=_TeardownFailsExecutor(), track=track)
    status = engine.submit(OpDefinition("run-td", (OpStep("s", "c", "run"),)))
    assert status is RunStatus.FAILED  # a leaked worker is not success
    events = [e.event_type for e in track.replay("run-td")]
    assert "teardown_failed" in events and "completed" not in events
    assert any(
        e.event_type == "failed" and e.payload.get("error") == "TeardownFailed"
        for e in track.replay("run-td")
    )
