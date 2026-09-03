from collab_hub_execution import (
    DurableWorkflowEngine,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    PauseRequest,
    RunStatus,
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

    assert engine.submit(op) is RunStatus.COMPLETED
    assert calls == [("run", "a"), ("run", "b")]
    assert [event.event_type for event in track.replay("run-1")].count("step_completed") == 2


def test_paused_op_resumes_from_track_after_engine_restart():
    state = {"paused": True}

    def handler(entry, value):
        if state["paused"]:
            raise PauseRequest("needs approval")
        return value

    track = InMemoryTrackStore()
    op = OpDefinition("run-2", (OpStep("approval", "human-gated", "approve", "work"),))
    first = DurableWorkflowEngine(executor=InMemoryCogExecutor({"human-gated": handler}), track=track)
    assert first.submit(op) is RunStatus.PAUSED

    state["paused"] = False
    restarted = DurableWorkflowEngine(executor=InMemoryCogExecutor({"human-gated": handler}), track=track)
    assert restarted.signal("run-2", "approved") is RunStatus.COMPLETED
    assert restarted.observe("run-2") is RunStatus.COMPLETED


def test_engine_failure_is_recorded_and_worker_is_torn_down():
    executor = InMemoryCogExecutor({"broken": lambda _entry, _value: 1 / 0})
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track)

    assert engine.submit(OpDefinition("run-3", (OpStep("broken", "broken", "run"),))) is RunStatus.FAILED
    assert executor.torn_down == ["broken"]
    assert engine.observe("run-3") is RunStatus.FAILED
