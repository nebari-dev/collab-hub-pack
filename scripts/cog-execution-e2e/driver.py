"""In-cluster E2E driver: run a gated multi-step Op via the KubernetesCogExecutor.

Runs as a Job with the cog-executor ServiceAccount. It materializes real Cog
worker pods, runs a 2-step Op where step 2 is gated, approves the gate, and
asserts the Track. Exits 0 on success, 1 on failure — so it doubles as the
repo's reproduction base.
"""

from __future__ import annotations

import os
import sys

from collab_hub_execution import (
    DurableWorkflowEngine,
    InMemoryTrackStore,
    KubernetesCogExecutor,
    OpDefinition,
    OpStep,
    RunStatus,
)


def main() -> int:
    namespace = os.environ["E2E_NAMESPACE"]
    runner_image = os.environ["E2E_RUNNER_IMAGE"]

    executor = KubernetesCogExecutor(
        runner_image=runner_image, namespace=namespace, ready_timeout=120, poll_interval=2
    )
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=executor, track=track)

    op = OpDefinition(
        "e2e-run",
        (
            OpStep("research", "openteams/research", "run", {"topic": "kind e2e"}, digest="sha256:research"),
            OpStep("review", "openteams/gated-reviewer", "review", {"draft": "v1"}, digest="sha256:reviewer"),
        ),
    )

    def dump(label: str, status: object) -> None:
        print(f"{label}: {status}", flush=True)
        for event in track.replay("e2e-run"):
            extra = event.payload.get("error") or event.payload.get("reason") or ""
            print(f"  - {event.event_type} {extra}".rstrip(), flush=True)

    status = engine.submit(op)
    dump("after submit", status)
    assert status is RunStatus.PAUSED, f"expected PAUSED at the gate, got {status}"

    status = engine.signal("e2e-run", {"approved": True})
    dump("after approval", status)
    assert status is RunStatus.COMPLETED, f"expected COMPLETED after approval, got {status}"

    events = [e.event_type for e in track.replay("e2e-run")]
    print("track events:", events, flush=True)
    for required in ("submitted", "materialized", "paused", "step_completed", "completed"):
        assert required in events, f"missing {required!r} in Track"

    # both Cogs were materialized as real pods and step outputs recorded with digests
    materialized = [e for e in track.replay("e2e-run") if e.event_type == "materialized"]
    assert any(e.payload.get("digest") == "sha256:research" for e in materialized)
    assert any(e.payload.get("digest") == "sha256:reviewer" for e in materialized)

    print("E2E OK: real Cog worker pods materialized, gated Op ran and completed, Track asserted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
