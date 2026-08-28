from __future__ import annotations

import pytest

from collab_hub_api.execution import (
    CogClass,
    CogDefinition,
    InMemoryExecutor,
    InMemoryOrchestrator,
    InMemoryResolver,
    InMemoryTrackStore,
    Op,
    RunStatus,
)
from collab_hub_api.execution.in_memory import CapabilityUnavailableError, InvalidEntryPointError


def make_orchestrator(handler=None):
    cog = CogDefinition(
        name="default-harness",
        cog_class=CogClass.HARNESS,
        entry_points=("run",),
        provides=frozenset({"text-generation"}),
        requires=frozenset({"model"}),
    )
    model = CogDefinition(
        name="default-model",
        cog_class=CogClass.MODEL,
        entry_points=("infer",),
        provides=frozenset({"model"}),
    )
    executor = InMemoryExecutor(
        {
            "default-harness": handler or (lambda _entry, value: value),
            "default-model": lambda _entry, value: value,
        }
    )
    track = InMemoryTrackStore()
    orchestrator = InMemoryOrchestrator(
        executor=executor,
        resolver=InMemoryResolver(),
        track=track,
        candidates=(cog, model),
    )
    return orchestrator, executor, track


def test_op_walks_through_all_interfaces_and_derives_status_from_track():
    orchestrator, executor, track = make_orchestrator()

    snapshot = orchestrator.submit(
        Op(
            run_id="run-1",
            required_capabilities=frozenset({"text-generation"}),
            entry_point="run",
            input={"prompt": "hello"},
        )
    )

    assert snapshot.status is RunStatus.COMPLETED
    assert [event.kind for event in snapshot.events] == [
        "submitted",
        "materialized",
        "ready",
        "interaction_started",
        "idle",
        "completed",
    ]
    assert orchestrator.observe("run-1").status is RunStatus.COMPLETED
    assert track.replay("run-1") == snapshot.events
    assert executor.materialized == [("run-1", "default-harness")]
    assert executor.torn_down == ["default-harness"]


def test_resolver_uses_declared_capabilities_only():
    orchestrator, _executor, _track = make_orchestrator()

    with pytest.raises(CapabilityUnavailableError):
        orchestrator.submit(
            Op(
                run_id="run-2",
                required_capabilities=frozenset({"image-generation"}),
                entry_point="run",
            )
        )


def test_resolver_rejects_an_unsatisfied_cog_requirement():
    cog = CogDefinition(
        name="needs-model",
        cog_class=CogClass.HARNESS,
        entry_points=("run",),
        provides=frozenset({"text-generation"}),
        requires=frozenset({"missing-model"}),
    )

    with pytest.raises(CapabilityUnavailableError):
        InMemoryResolver().resolve({"text-generation"}, (cog,))


def test_interaction_uses_declared_entry_point():
    orchestrator, _executor, _track = make_orchestrator()

    with pytest.raises(InvalidEntryPointError):
        orchestrator.submit(
            Op(run_id="run-3", required_capabilities=frozenset({"text-generation"}), entry_point="opaque-step")
        )
