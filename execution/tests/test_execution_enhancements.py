"""Budget enforcement, bounded revise loops, and per-step digests on the engine.

These complete intents that existed as unused primitives: lifecycle budgets were
defined but never enforced in a run, and the Track lacked per-step digests and a
bounded revise loop.
"""

from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_execution import (
    DurableWorkflowEngine,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    Problem,
    ResultEnvelope,
    RunBudget,
    RunState,
    TrackEvent,
    derive_run_status,
)
from collab_hub_execution.orchestration import _NO_SIGNAL, _serialize_op

SIGN_OFF = Gate(escalate="always")


def _review(value=None, *, tokens=None):
    """An answer whose error problem makes the step's default Gate escalate it."""
    usage = None if tokens is None else {"tokens": tokens}
    return ResultEnvelope.success(value, usage=usage, problems=[Problem("review", "needs another look")])


def _decide(engine, run_id, outcome, *findings):
    escalation = engine.open_escalation(run_id)["escalation"]
    return engine.decide(run_id, escalation=escalation, actor="alice", outcome=outcome, findings=list(findings))


def test_token_budget_survives_restart_and_stops_the_run():
    """Budget is reconstructed from the Track, so it holds across an engine restart."""
    def always_usage(entry, value):
        return ResultEnvelope.success({"result": value}, usage={"tokens": 60})

    def review_then_usage(entry, value, *, signal=_NO_SIGNAL):
        if signal is _NO_SIGNAL:
            return _review({"result": value}, tokens=0)
        return ResultEnvelope.success({"result": value}, usage={"tokens": 60})

    track = InMemoryTrackStore()
    budget = RunBudget(max_tokens=100)
    op = OpDefinition("run-budget", (OpStep("s1", "a", "run", "x"), OpStep("s2", "b", "run", "y")))

    def engine():
        return DurableWorkflowEngine(
            executor=InMemoryCogExecutor({"a": always_usage, "b": review_then_usage}), track=track, budget=budget,
        )

    assert engine().submit(op) is RunState.WAITING_AT_GATE  # s1 consumes 60 (<100); s2 escalates
    # fresh engine: reconstructed tracker already holds s1's 60; s2 sent back spends 60 -> 120 > 100
    assert _decide(engine(), "run-budget", "send_back", "tighten it") is RunState.BUDGET_EXCEEDED
    assert any(e.event_type == "budget_exceeded" for e in track.replay("run-budget"))


def test_duration_budget_stops_run_before_any_step_as_timed_out():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"a": lambda e, v: v}),
        track=track,
        budget=RunBudget(max_duration=timedelta(0)),
    )
    # a duration overrun is a budget stop whose dimension says so, distinct from token/cost overspend
    assert engine.submit(OpDefinition("run-dur", (OpStep("s", "a", "run"),))) is RunState.BUDGET_EXCEEDED
    [stop] = [e for e in track.replay("run-dur") if e.event_type == "budget_exceeded"]
    assert stop.payload["dimension"] == "duration"


def test_bounded_revise_loop_fails_after_max_revisions():
    runs = []

    def always_needs_review(entry, value, *, signal=None):
        runs.append(signal)
        return _review(value)

    track = InMemoryTrackStore()
    op = OpDefinition("run-revise", (OpStep("draft", "writer", "revise", "v0"),))

    def engine():
        return DurableWorkflowEngine(
            executor=InMemoryCogExecutor({"writer": always_needs_review}), track=track, max_revisions=2,
        )

    assert engine().submit(op) is RunState.WAITING_AT_GATE                          # escalation 1
    assert _decide(engine(), "run-revise", "send_back", "fix a") is RunState.WAITING_AT_GATE  # revision 1
    assert _decide(engine(), "run-revise", "send_back", "fix b") is RunState.WAITING_AT_GATE  # revision 2
    assert _decide(engine(), "run-revise", "send_back", "fix c") is RunState.FAILED  # would be revision 3
    assert any(
        e.event_type == "failed" and e.payload.get("error") == "revise_limit_exceeded"
        for e in track.replay("run-revise")
    )
    # max_revisions=2 is two revisions: the step runs three times, and the third send back fails the run.
    assert runs == [None, ["fix a"], ["fix b"]]


@pytest.mark.parametrize("max_revisions", [1, 2])
def test_an_approval_is_never_charged_as_a_revision(max_revisions):
    signals = []

    def reviewer(entry, value, *, signal=None):
        signals.append(signal)
        return _review({"draft": len(signals)})

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": reviewer}), track=track,
                                   max_revisions=max_revisions)
    op = OpDefinition("run-approve", (OpStep("s", "c", "review", "draft"),))
    assert engine.submit(op) is RunState.WAITING_AT_GATE
    for _ in range(max_revisions):
        assert _decide(engine, "run-approve", "send_back", "fix") is RunState.WAITING_AT_GATE
    # The last revision the limit allows is approved, and the run completes with it, as the approver saw it.
    assert _decide(engine, "run-approve", "approve") is RunState.COMPLETED
    assert len(signals) == max_revisions + 1
    [completed] = [e for e in track.replay("run-approve") if e.event_type == "step_completed"]
    assert completed.payload["payload"] == {"draft": max_revisions + 1}


@pytest.mark.parametrize("ending,state", [
    (("gate_decided", {"step": "s", "outcome": "reject", "actor": "alice"}), "rejected"),
    (("cancelled", {"actor": "alice"}), "cancelled"),
], ids=["rejected", "cancelled"])
def test_a_rejected_or_cancelled_run_is_refused_a_retry_before_anything_is_read_or_written(ending, state):
    track = InMemoryTrackStore()
    for kind, payload in (("op_submitted", {"op": {"run_id": "r", "steps": []}}), ("run_picked_up", {}),
                          ("gate_escalated", {"step": "s"}), ending):
        track.append(TrackEvent(run_id="r", event_type=kind, payload=payload))
    before = track.replay("r")
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({}), track=track)
    with pytest.raises(ValueError, match=f"was {state}; nothing to retry"):
        engine.retry("r")
    assert track.replay("r") == before


class CountingTrack(InMemoryTrackStore):
    """Counts how often a call reads the run's Track."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def replay(self, run_id, *, after_sequence=0):
        self.reads += 1
        return super().replay(run_id, after_sequence=after_sequence)


def test_a_call_reads_the_track_once_and_advances_on_what_it_read_and_wrote():
    track = CountingTrack()

    def reviewer(entry, value, *, signal=None):
        return ResultEnvelope.success(value) if signal else _review(value)

    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": reviewer}), track=track)
    op = OpDefinition("run-reads", (OpStep("s0", "c", "run"), OpStep("s1", "c", "run", gate=Gate(escalate="never"))))
    assert engine.submit(op) is RunState.WAITING_AT_GATE
    assert track.reads == 1
    track.reads = 0
    assert _decide(engine, "run-reads", "send_back", "again") is RunState.COMPLETED
    assert track.reads == 2  # open_escalation, then the decision, which advances on what it wrote


def test_step_digest_is_recorded_and_survives_restart():
    track = InMemoryTrackStore()
    op = OpDefinition("run-digest", (OpStep("s", "cog", "run", "x", digest="sha256:abc", gate=SIGN_OFF),))

    def engine():
        # A handler that is sent back receives the findings as a keyword `signal`.
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"cog": lambda e, v, signal=None: v}), track=track)

    engine().submit(op)
    started = [e for e in track.replay("run-digest") if e.event_type == "step_started"]
    assert started and started[0].payload.get("digest") == "sha256:abc"

    # restart: the op is reconstructed from the Track alone; the digest must round-trip
    assert _decide(engine(), "run-digest", "send_back", "again") is RunState.WAITING_AT_GATE
    assert _decide(engine(), "run-digest", "approve") is RunState.COMPLETED
    materialized = [e for e in track.replay("run-digest") if e.event_type == "materialized"]
    assert len(materialized) == 2 and all(e.payload.get("digest") == "sha256:abc" for e in materialized)


# --- failure handling (#5), idempotency key (#3), duplicate step names (#7) ---


class _FailingMaterializeExecutor:
    def materialize(self, cog, run_id, instance=""):
        raise RuntimeError("api down")

    def teardown(self, worker):  # never called (materialize failed)
        raise AssertionError("teardown should not run when materialize failed")


def test_materialize_failure_is_recorded_durably_not_left_hanging():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=_FailingMaterializeExecutor(), track=track)
    status = engine.submit(OpDefinition("run-mat", (OpStep("s", "c", "run"),)))
    assert status is RunState.FAILED
    events = [e.event_type for e in track.replay("run-mat")]
    assert "failed" in events and "completed" not in events  # terminal, not stuck


class _CapturingWorker:
    def __init__(self):
        self.keys = []

    def interact(self, entry_point, input=None, idempotency_key=None):
        self.keys.append(idempotency_key)
        return ResultEnvelope.success({"ok": True})


class _CapturingExecutor:
    def __init__(self, worker):
        self.worker = worker
        self.torn = 0

    def materialize(self, cog, run_id, instance=""):
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

    def materialize(self, cog, run_id, instance=""):
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
        self._seen[idempotency_key] = ResultEnvelope.success({"done": idempotency_key})
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
    assert engine.observe("run-idem") not in (RunState.COMPLETED, RunState.FAILED)
    assert engine.submit(op) is RunState.COMPLETED  # resume re-drives with the SAME key
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
    assert engine.submit(op) is RunState.FAILED
    before = len(track.replay("run-term"))
    assert engine.submit(op) is RunState.FAILED  # immutable: no re-drive
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
            return ResultEnvelope.success({"ok": True})

    def materialize(self, cog, run_id, instance=""):
        return self._Worker(self)

    def teardown(self, worker):
        pass


def test_retry_re_drives_a_failed_run_under_a_fresh_key():
    ex = _RetryProbeExecutor()
    engine = DurableWorkflowEngine(executor=ex, track=InMemoryTrackStore())
    op = OpDefinition("run-retry", (OpStep("s", "c", "run"),))
    assert engine.submit(op) is RunState.FAILED
    assert engine.retry("run-retry") is RunState.COMPLETED
    # explicit retry is a new attempt -> fresh key, so the work genuinely re-runs
    assert ex.keys == ["run-retry:s:0", "run-retry:s:1"]


def test_retry_rejects_a_non_terminal_run():
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=InMemoryTrackStore())
    assert engine.submit(OpDefinition("run-waiting", (OpStep("s", "c", "run", gate=SIGN_OFF),))) \
        is RunState.WAITING_AT_GATE
    with pytest.raises(ValueError):
        engine.retry("run-waiting")


def test_retry_rejects_a_completed_run():
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=InMemoryTrackStore()
    )
    assert engine.submit(OpDefinition("run-ok", (OpStep("s", "c", "run"),))) is RunState.COMPLETED
    with pytest.raises(ValueError):  # nothing to retry; re-running finished work is a new Op
        engine.retry("run-ok")


# --- a paused run resumes only through signal(), never a re-submit ---


def test_re_submitting_a_run_waiting_at_a_gate_does_not_resume_it_behind_the_gate():
    calls = {"n": 0}

    def cog(entry, value):
        calls["n"] += 1
        return value

    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": cog}), track=track)

    op = OpDefinition("run-gate", (OpStep("s", "c", "run", "x", gate=SIGN_OFF),))
    assert engine().submit(op) is RunState.WAITING_AT_GATE
    assert calls["n"] == 1
    # a re-submit must NOT re-invoke the gated step (that would bypass the gate)
    assert engine().submit(op) is RunState.WAITING_AT_GATE
    assert calls["n"] == 1
    # the gate opens only through a decision; an approval takes the result as it is
    assert _decide(engine(), "run-gate", "approve") is RunState.COMPLETED
    assert calls["n"] == 1


# --- duration budget must survive a crash immediately after submission ---


def test_duration_budget_survives_a_crash_after_submission():
    track = InMemoryTrackStore()
    op = OpDefinition("run-crash-budget", (OpStep("s", "c", "run"),))
    # Simulate a crash after op_submitted committed, before advancement.
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
    # 5-minute duration budget is correctly seen as exceeded (a timeout)
    assert engine.submit(op) is RunState.BUDGET_EXCEEDED


# --- signal values are durable and recovered from the Track ---


def test_send_back_findings_are_durable_across_a_crash_mid_revision():
    seen = []
    crash = {"once": True}

    def handler(entry, value, *, signal=_NO_SIGNAL):
        if signal is _NO_SIGNAL:
            return _review(value)
        seen.append((value, signal))
        if crash["once"]:
            crash["once"] = False
            raise SystemExit("crash while revising, before completion")
        return ResultEnvelope.success({"ok": True})

    track = InMemoryTrackStore()

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": handler}), track=track)

    op = OpDefinition("run-sig", (OpStep("s", "c", "review", "draft-v1"),))
    assert engine().submit(op) is RunState.WAITING_AT_GATE
    with pytest.raises(SystemExit):  # crash while re-running with the findings
        _decide(engine(), "run-sig", "send_back", "cite the source")
    # Recovery must preserve both input and findings from the Track.
    assert engine().submit(op) is RunState.COMPLETED
    assert seen == [("draft-v1", ["cite the source"]), ("draft-v1", ["cite the source"])]


def test_a_send_back_with_no_findings_still_re_runs_the_step_with_a_signal():
    seen = []

    def handler(entry, value, *, signal=_NO_SIGNAL):
        if signal is _NO_SIGNAL:
            return _review(value)
        seen.append((value, signal))
        return ResultEnvelope.success({"ok": True})

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": handler}), track=track)
    op = OpDefinition("run-none", (OpStep("s", "c", "run", "GATE"),))
    assert engine.submit(op) is RunState.WAITING_AT_GATE
    assert _decide(engine, "run-none", "send_back") is RunState.COMPLETED
    assert seen == [("GATE", [])]


# --- idempotency keys are injective even when ids contain the delimiter ---


class _KeyCapture:
    class _Worker:
        cog = "c"

        def __init__(self, outer):
            self._outer = outer

        def interact(self, entry_point, input=None, idempotency_key=None):
            self._outer.keys.append(idempotency_key)
            return ResultEnvelope.success({"ok": True})

    def __init__(self):
        self.keys = []

    def materialize(self, cog, run_id, instance=""):
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
    kinds = ["op_submitted", "run_picked_up", *per_step, "step_completed"]
    events = [TrackEvent(run_id="r", event_type=t) for t in kinds]
    assert derive_run_status(events) is RunState.RUNNING  # between steps, still progressing
    events.append(TrackEvent(run_id="r", event_type="completed"))
    assert derive_run_status(events) is RunState.COMPLETED  # the final event still wins


# --- budget limits are inclusive: reaching exactly the max stops the run ---


def test_a_budget_stop_keeps_an_escalated_result_rather_than_discarding_it():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v, signal=None: _review({"draft": v}, tokens=60)}),
        track=track,
        budget=RunBudget(max_tokens=50),
    )
    op = OpDefinition("run-paid", (OpStep("s", "c", "run", "x"),))
    # The interaction crossed the budget and its result needs review: the escalation is
    # recorded, so the work that was paid for is on the Track.
    assert engine.submit(op) is RunState.WAITING_AT_GATE
    escalation = engine.open_escalation("run-paid")
    assert ResultEnvelope.parse(escalation["envelope"]).payload == {"draft": "x"}
    # Approving spends nothing and keeps it; the run then stops on its budget.
    assert engine.decide("run-paid", escalation=escalation["escalation"], actor="alice",
                         outcome="approve") is RunState.BUDGET_EXCEEDED
    kinds = [e.event_type for e in track.replay("run-paid")]
    assert kinds.index("step_completed") < kinds.index("budget_exceeded")


def test_budget_boundary_is_inclusive_so_exact_max_is_exceeded():
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, usage={"tokens": 60})}),
        track=InMemoryTrackStore(),
        budget=RunBudget(max_tokens=60),
    )
    assert engine.submit(OpDefinition("run-exact", (OpStep("s", "c", "run"),))) is RunState.BUDGET_EXCEEDED


class _TeardownFailsExecutor:
    class _Worker:
        cog = "c"

        def interact(self, entry_point, input=None, idempotency_key=None):
            return ResultEnvelope.success({"ok": True})

    def materialize(self, cog, run_id, instance=""):
        return self._Worker()

    def teardown(self, worker):
        raise RuntimeError("delete failed")


def test_teardown_failure_fails_the_run_rather_than_reporting_success():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=_TeardownFailsExecutor(), track=track)
    status = engine.submit(OpDefinition("run-td", (OpStep("s", "c", "run"),)))
    assert status is RunState.FAILED  # a leaked worker is not success
    events = [e.event_type for e in track.replay("run-td")]
    assert "teardown_failed" in events and "completed" not in events
    assert any(
        e.event_type == "failed" and e.payload.get("error") == "TeardownFailed"
        for e in track.replay("run-td")
    )


def test_a_failed_worker_that_cannot_be_reclaimed_is_recorded_on_the_run_not_around_its_machine():
    class _Broken(_TeardownFailsExecutor._Worker):
        def interact(self, entry_point, input=None, idempotency_key=None):
            raise ConnectionError("pod went away")

    class _Executor(_TeardownFailsExecutor):
        def materialize(self, cog, run_id, instance=""):
            return _Broken()

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=_Executor(), track=track)
    assert engine.submit(OpDefinition("run-lost", (OpStep("s", "c", "run"),))) is RunState.FAILED
    events = track.replay("run-lost")
    assert "teardown_failed" not in [e.event_type for e in events]  # the worker had already failed
    [failed] = [e for e in events if e.event_type == "failed"]
    assert failed.payload == {"step": "s", "error": "TeardownFailed", "reason": "RuntimeError",
                              "worker_error": "ConnectionError"}
