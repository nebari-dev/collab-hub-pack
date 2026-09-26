"""Track event schema v1 as the engine writes it: what produced each result, and who signed."""

from collab_hub_execution import (
    SCHEMA_VERSION,
    DurableWorkflowEngine,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    Problem,
    ResultEnvelope,
    RunState,
    SqliteTrackStore,
    TrackEvent,
)
from collab_hub_execution.orchestration import MESSAGE_MAX_CHARS

ERROR = Problem("grounding", "quote not verbatim", "error")


def _events(track, run_id, kind):
    return [e for e in track.replay(run_id) if e.event_type == kind]


# --- step_completed: the Track alone names what produced the result ----------------------


def test_step_completed_names_the_cog_the_binding_the_problems_the_usage_and_the_frames():
    track = InMemoryTrackStore()
    envelope = ResultEnvelope.success({"answer": 42}, usage={"tokens": 3}, binding={"model": "m1"},
                                      problems=[Problem("style", "long", "warn")])
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: envelope}), track=track)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", "in", digest="sha256:abc"),))) \
        is RunState.COMPLETED
    [completed] = _events(track, "r", "step_completed")
    assert completed.schema == SCHEMA_VERSION
    assert completed.payload == {
        "step": "s", "attempt": 0, "cog": "c", "digest": "sha256:abc", "usage": {"tokens": 3}, "frames": [],
        "binding": {"model": "m1"}, "problems": [{"check": "style", "detail": "long", "severity": "warn"}],
        "payload": {"answer": 42},
    }
    assert all(e.schema == SCHEMA_VERSION for e in track.replay("r"))


def test_a_large_payload_is_kept_by_reference_and_a_small_one_inline():
    track = InMemoryTrackStore()
    big = {"text": "x" * 200}
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=track,
                                   payload_inline_max_bytes=100)
    op = OpDefinition("r", (OpStep("small", "c", "run", {"n": 1}), OpStep("large", "c", "run", big)))
    assert engine.submit(op) is RunState.COMPLETED
    small, large = _events(track, "r", "step_completed")
    assert small.payload["payload"] == {"n": 1} and "payload_ref" not in small.payload
    assert "payload" not in large.payload
    # Kept under the attempt's idempotency key, so recovering the attempt rewrites the same row.
    assert large.payload["payload_ref"] == "r:large:0"
    assert track.get_payload("r:large:0") == big


def test_an_approved_result_is_completed_with_the_same_record_shape():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, usage={"tokens": 1})}), track=track,
    )
    op = OpDefinition("r", (OpStep("s", "c", "run", "draft", gate=Gate(escalate="always")),))
    assert engine.submit(op) is RunState.WAITING_AT_GATE
    escalation = engine.open_escalation("r")
    assert engine.decide("r", escalation=escalation["escalation"], actor="alice", outcome="approve") \
        is RunState.COMPLETED
    [completed] = _events(track, "r", "step_completed")
    assert completed.payload["payload"] == "draft" and completed.payload["usage"] == {"tokens": 1}
    assert completed.payload["escalation"] == escalation["escalation"]
    assert completed.payload["cog"] == "c" and completed.payload["attempt"] == 0
    [decided] = _events(track, "r", "gate_decided")
    assert decided.payload["actor"] == "alice" and decided.payload["outcome"] == "approve"


# --- step_failed: more than a class name ---------------------------------------------------


def test_a_broken_interaction_records_the_step_s_failure_with_its_key_worker_and_message():
    def boom(entry, value):
        raise RuntimeError("the model host returned nothing usable " + "!" * 2000)

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": boom}), track=track)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", digest="sha256:abc"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["step"] == "s" and failed_step.payload["attempt"] == 0
    assert failed_step.payload["key"] == "r:s:0" and failed_step.payload["cog"] == "c"
    assert failed_step.payload["digest"] == "sha256:abc" and failed_step.payload["error"] == "RuntimeError"
    # The class names the failure; its message says what happened, bounded.
    assert failed_step.payload["message"].startswith("the model host returned nothing usable ")
    assert len(failed_step.payload["message"]) == MESSAGE_MAX_CHARS
    kinds = [e.event_type for e in track.replay("r")]
    assert kinds.index("step_failed") < kinds.index("failed")


def test_an_error_envelope_records_its_code_detail_and_problems_on_the_step_s_failure():
    answer = ResultEnvelope.failure("model-call-failed", "upstream 502 " + "x" * 2000,
                                    problems=[Problem("input", "too long")], binding={"model": "m1"})
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: answer}), track=track)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "model-call-failed"
    assert failed_step.payload["message"].startswith("upstream 502 ") and \
        len(failed_step.payload["message"]) == MESSAGE_MAX_CHARS
    assert failed_step.payload["problems"] == [{"check": "input", "detail": "too long", "severity": "error"}]
    assert failed_step.payload["binding"] == {"model": "m1"}


def test_a_usage_failure_records_its_reason_on_the_step_s_failure():
    from collab_hub_execution import RunBudget

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v)}),
                                   track=track, budget=RunBudget(max_tokens=10))
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "UsageUnavailable"
    assert failed_step.payload["message"] == "missing tokens usage for configured budget"


def test_a_worker_that_cannot_be_torn_down_records_the_step_s_failure_too():
    class Executor:
        class Worker:
            cog = "c"

            def interact(self, entry_point, input=None, idempotency_key=None):
                return ResultEnvelope.success("ok")

        def materialize(self, cog, run_id, instance=""):
            return self.Worker()

        def teardown(self, worker):
            raise RuntimeError("delete failed")

    track = InMemoryTrackStore()
    assert DurableWorkflowEngine(executor=Executor(), track=track).submit(
        OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "TeardownFailed"
    assert failed_step.payload["message"] == "RuntimeError: the worker could not be torn down"


# --- a Track written before v1 still drives ------------------------------------------------


def test_the_engine_reads_a_pre_v1_track_and_drives_it_on():
    track = InMemoryTrackStore()
    op = {"run_id": "old", "steps": [{"name": "s", "cog": "c", "entry_point": "run", "input": "draft",
                                      "digest": None}]}
    for kind, payload in (("submitted", {"op": op}), ("step_started", {"step": "s", "attempt": 0}),
                          ("paused", {"step": "s", "reason": "cog requested a pause"})):
        track.append(TrackEvent(run_id="old", event_type=kind, payload=payload, schema=0))
    calls = []
    def cog(entry, value, signal=None):
        calls.append(signal)
        return ResultEnvelope.success(value)

    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": cog}), track=track)
    assert engine.observe("old") is RunState.WAITING_AT_GATE
    assert engine.open_escalation("old")["escalation"] is None
    assert engine.decide("old", escalation=None, actor="alice", outcome="send_back", findings=["again"]) \
        is RunState.COMPLETED
    assert calls == [["again"]]
    # The Track is not rewritten: the old rows keep their version, the new ones carry v1.
    versions = [e.schema for e in track.replay("old")]
    assert versions[:3] == [0, 0, 0] and set(versions[3:]) == {SCHEMA_VERSION}


# --- status is derived from the Track after a restart --------------------------------------


def test_status_and_decisions_survive_a_restart_over_a_sqlite_track(tmp_path):
    path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(path)
    op = OpDefinition("r", (OpStep("s", "c", "run", "draft", gate=Gate(escalate="always")),))

    def engine():  # a new process: a new store object, a new engine, the same file
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=SqliteTrackStore(path))

    assert engine().submit(op) is RunState.WAITING_AT_GATE
    assert engine().observe("r") is RunState.WAITING_AT_GATE
    escalation = engine().open_escalation("r")["escalation"]
    assert engine().decide("r", escalation=escalation, actor="alice", outcome="approve") is RunState.COMPLETED
    assert engine().observe("r") is RunState.COMPLETED
    [completed] = [e for e in SqliteTrackStore(path).replay("r") if e.event_type == "step_completed"]
    assert completed.payload["payload"] == "draft"


def test_the_sqlite_schema_is_created_once_and_the_file_s_directory_with_it(tmp_path):
    path = tmp_path / "deeper" / "track.sqlite"
    SqliteTrackStore.ensure_schema(path)
    SqliteTrackStore.ensure_schema(path)  # idempotent
    store = SqliteTrackStore(path)
    store.append(TrackEvent(run_id="r", event_type="op_submitted"))
    assert [e.event_type for e in store.replay("r")] == ["op_submitted"]


# --- the review on #158 --------------------------------------------------------------------


class _CountingPayloads(InMemoryTrackStore):
    def __init__(self):
        super().__init__()
        self.puts = []

    def put_payload(self, run_id, ref, rendered):
        self.puts.append((run_id, ref, len(rendered)))
        super().put_payload(run_id, ref, rendered)


def test_an_escalated_large_result_is_kept_once_by_reference_and_approved_from_there():
    track = _CountingPayloads()
    big = {"text": "x" * 500}
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=track,
                                   payload_inline_max_bytes=100)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", big, gate=Gate(escalate="always")),))) \
        is RunState.WAITING_AT_GATE
    [escalated] = _events(track, "r", "gate_escalated")
    assert escalated.payload["payload_ref"] == "r:s:0"
    assert escalated.payload["envelope"]["payload"] is None  # not inline on the Track
    assert len(escalated.payload["envelope"]["payload_digest"]) == 64
    escalation = engine.open_escalation("r")
    assert escalation["payload_ref"] == "r:s:0" and track.get_payload("r:s:0") == big
    assert engine.decide("r", escalation=escalation["escalation"], actor="alice", outcome="approve") \
        is RunState.COMPLETED
    [completed] = _events(track, "r", "step_completed")
    assert completed.payload["payload_ref"] == "r:s:0" and "payload" not in completed.payload
    assert [ref for _, ref, _ in track.puts] == ["r:s:0"]  # kept once, not again on approval


def test_two_large_results_differing_only_in_their_payload_have_different_digests_to_decide_on():
    from collab_hub_execution import envelope_digest

    def escalate(value):
        track = InMemoryTrackStore()
        engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=track,
                                       payload_inline_max_bytes=10)
        engine.submit(OpDefinition("r", (OpStep("s", "c", "run", value, gate=Gate(escalate="always")),)))
        return envelope_digest(engine.open_escalation("r")["envelope"])

    assert escalate({"text": "a" * 50}) != escalate({"text": "b" * 50})


def test_a_recovered_attempt_rewrites_its_payload_instead_of_leaving_another_one():
    class CrashOnce(_CountingPayloads):
        crashed = False

        def append(self, event):
            if event.event_type == "step_completed" and not CrashOnce.crashed:
                CrashOnce.crashed = True
                raise SystemExit("the process stopped after keeping the payload")
            return super().append(event)

    track = CrashOnce()
    op = OpDefinition("r", (OpStep("s", "c", "run", {"text": "x" * 500}),))

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=track,
                                     payload_inline_max_bytes=100)

    try:
        engine().submit(op)
    except SystemExit:
        pass
    assert engine().submit(op) is RunState.COMPLETED
    assert [ref for _, ref, _ in track.puts] == ["r:s:0", "r:s:0"]  # the same row, rewritten
    assert list(track._payloads) == ["r:s:0"]


def test_a_payload_that_is_not_json_fails_the_step_durably():
    from datetime import datetime

    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(
        executor=InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success({"at": datetime(2026, 1, 1)})}),
        track=track,
    )
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "EnvelopeInvalid"
    assert failed_step.payload["message"].startswith("the payload is not JSON")


def test_a_failed_teardown_keeps_the_step_s_own_failure():
    class Executor:
        class Worker:
            cog = "c"

            def interact(self, entry_point, input=None, idempotency_key=None):
                raise RuntimeError("image pull backoff: registry unreachable")

        def materialize(self, cog, run_id, instance=""):
            return self.Worker()

        def teardown(self, worker):
            raise ConnectionError("delete failed")

    track = InMemoryTrackStore()
    assert DurableWorkflowEngine(executor=Executor(), track=track).submit(
        OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "RuntimeError"
    assert failed_step.payload["message"] == "image pull backoff: registry unreachable"
    assert failed_step.payload["teardown_error"] == "ConnectionError"
    [failed] = _events(track, "r", "failed")
    assert failed.payload["error"] == "TeardownFailed" and failed.payload["worker_error"] == "RuntimeError"


def test_a_failed_teardown_after_an_error_envelope_keeps_its_code_and_problems():
    answer = ResultEnvelope.failure("model-call-failed", "upstream 502", problems=[Problem("input", "too long")])

    class Executor:
        class Worker:
            cog = "c"

            def interact(self, entry_point, input=None, idempotency_key=None):
                return answer

        def materialize(self, cog, run_id, instance=""):
            return self.Worker()

        def teardown(self, worker):
            raise RuntimeError("delete failed")

    track = InMemoryTrackStore()
    DurableWorkflowEngine(executor=Executor(), track=track).submit(OpDefinition("r", (OpStep("s", "c", "run"),)))
    [failed_step] = _events(track, "r", "step_failed")
    assert failed_step.payload["error"] == "model-call-failed" and failed_step.payload["message"] == "upstream 502"
    assert failed_step.payload["problems"] == [{"check": "input", "detail": "too long", "severity": "error"}]
    assert failed_step.payload["teardown_error"] == "RuntimeError"


def test_the_run_s_failure_reason_is_bounded_like_the_step_s():
    answer = ResultEnvelope.failure("model-call-failed", "y" * 200_000)
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: answer}), track=track)
    engine.submit(OpDefinition("r", (OpStep("s", "c", "run"),)))
    [failed] = _events(track, "r", "failed")
    assert len(failed.payload["reason"]) == MESSAGE_MAX_CHARS
