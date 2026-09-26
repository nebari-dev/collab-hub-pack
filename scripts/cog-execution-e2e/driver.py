"""In-cluster E2E driver: run a gated multi-step Op via the KubernetesCogExecutor.

Runs as a Job with the cog-executor ServiceAccount. It materializes real Cog
worker pods, runs a 2-step Op whose second step declares a sign-off Gate, sends
that step back once with findings, approves the revision, and asserts the Track. Exits 0 on success, 1 on failure — so it doubles as the
repo's reproduction base.
"""

from __future__ import annotations

import os
import sys

from collab_hub_execution import (
    DurableWorkflowEngine,
    Gate,
    InMemoryTrackStore,
    KubernetesCogExecutor,
    OpDefinition,
    OpStep,
    RunBudget,
    RunState,
)


def main() -> int:
    namespace = os.environ["E2E_NAMESPACE"]
    runner_image = os.environ["E2E_RUNNER_IMAGE"]

    executor = KubernetesCogExecutor(
        runner_image=runner_image,
        namespace=namespace,
        # Materialize a NetworkPolicy admitting only this driver (the stand-in hub)
        # to each worker. kind's default CNI does not enforce NetworkPolicy, so this
        # exercises the secure code path; a policy-enforcing CNI enforces it.
        allow_ingress_from={"app": "op-driver"},
        ready_timeout=120,
        poll_interval=2,
    )
    track = InMemoryTrackStore()
    # Three interactions of 10 tokens: research, the review, and its revision.
    engine = DurableWorkflowEngine(executor=executor, track=track, budget=RunBudget(max_tokens=35))

    op = OpDefinition(
        "e2e-run",
        (
            OpStep("research", "openteams/research", "run", {"topic": "kind e2e"}, digest="sha256:research"),
            OpStep("review", "openteams/reviewer", "review", {"draft": "v1"}, digest="sha256:reviewer",
                   gate=Gate(escalate="always")),
        ),
    )

    def dump(label: str, status: object) -> None:
        print(f"{label}: {status}", flush=True)
        for event in track.replay("e2e-run"):
            extra = event.payload.get("error") or event.payload.get("reason") or ""
            print(f"  - {event.event_type} {extra}".rstrip(), flush=True)

    status = engine.submit(op)
    dump("after submit", status)
    assert status is RunState.WAITING_AT_GATE, f"expected WAITING_AT_GATE, got {status}"

    first = engine.open_escalation("e2e-run")["escalation"]
    status = engine.decide("e2e-run", escalation=first, actor="e2e", outcome="send_back", findings=["cite a source"])
    dump("after send back", status)
    assert status is RunState.WAITING_AT_GATE, f"expected the revision at the Gate, got {status}"

    revision = engine.open_escalation("e2e-run")["escalation"]
    assert revision != first, "a revision is a new escalation"
    status = engine.decide("e2e-run", escalation=revision, actor="e2e", outcome="approve")
    dump("after approval", status)
    assert status is RunState.COMPLETED, f"expected COMPLETED after approval, got {status}"

    events = [e.event_type for e in track.replay("e2e-run")]
    print("track events:", events, flush=True)
    for required in ("op_submitted", "materialized", "gate_escalated", "gate_decided", "step_completed",
                     "completed"):
        assert required in events, f"missing {required!r} in Track"

    # both Cogs were materialized as real pods and step outputs recorded with digests
    materialized = [e for e in track.replay("e2e-run") if e.event_type == "materialized"]
    assert any(e.payload.get("digest") == "sha256:research" for e in materialized)
    assert any(e.payload.get("digest") == "sha256:reviewer" for e in materialized)

    review = next(
        e.payload["payload"] for e in track.replay("e2e-run")
        if e.event_type == "step_completed" and e.payload["step"] == "review"
    )
    # The approved revision is the one that ran with the findings, delivered to the pod as its signal.
    assert review["echo"] == {"draft": "v1"}
    assert review["signal"] == ["cite a source"]
    assert sum(
        e.payload["usage"]["tokens"] for e in track.replay("e2e-run")
        if e.event_type == "interaction_usage"
    ) == 30  # research, the review, and its revision

    print("E2E OK: real Cog worker pods materialized, a Gate sent a step back and approved it, Track asserted",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
