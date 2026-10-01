from collab_hub_execution import (
    DurableWorkflowEngine,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    RunState,
)


def test_multi_step_op_interacts_with_each_cog_and_completes():
    calls = []
    executor = InMemoryCogExecutor(
        {
            "first": lambda entry, value: calls.append((entry, value)) or "one",
            "second": lambda entry, value: calls.append((entry, value)) or "two",
        }
    )
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track)
    op = OpDefinition(
        "run-1",
        (OpStep("first-step", "first", "run", "a"), OpStep("second-step", "second", "run", "b")),
    )

    assert engine.submit(op) is RunState.COMPLETED
    assert calls == [("run", "a"), ("run", "b")]
    assert [event.event_type for event in track.replay("run-1")].count("step_completed") == 2


def test_a_run_waiting_at_a_gate_is_approved_by_another_engine_after_a_restart():
    calls = []
    track = InMemoryTrackStore()
    op = OpDefinition("run-2", (OpStep("draft", "writer", "write", "work", gate=Gate(escalate="always")),))

    def engine():
        return DurableWorkflowEngine(executor=InMemoryCogExecutor({"writer": lambda e, v: calls.append(v) or v}),
                                     track=track)

    assert engine().submit(op) is RunState.WAITING_AT_GATE
    escalation = engine().open_escalation("run-2")["escalation"]
    assert engine().decide("run-2", escalation=escalation, actor="alice", outcome="approve") is RunState.COMPLETED
    assert engine().observe("run-2") is RunState.COMPLETED
    assert calls == ["work"]  # approved as it was: the step did not run again


def test_engine_failure_is_recorded_and_worker_is_torn_down():
    executor = InMemoryCogExecutor({"broken": lambda _entry, _value: 1 / 0})
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track)

    assert engine.submit(OpDefinition("run-3", (OpStep("broken", "broken", "run"),))) is RunState.FAILED
    assert executor.torn_down == ["broken"]
    assert engine.observe("run-3") is RunState.FAILED
