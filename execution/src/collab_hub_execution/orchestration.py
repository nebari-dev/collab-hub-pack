"""Durable multi-step Op orchestration behind a replaceable engine contract.

``DurableWorkflowEngine`` is the contract's reference engine. It makes no lifecycle
decision of its own: every method delegates to the ``LifecycleRunner``
(``runner.py``), where the lifecycle lives as step functions. The Op, its steps
and the seam's types are in ``ops.py``; they are re-exported here for callers
that import them from this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .lifecycle import BudgetTracker, RunBudget
from .ops import (  # noqa: F401 - re-exported for callers that import them from here
    _NO_SIGNAL,
    CogExecutor,
    CogWorker,
    InMemoryCogExecutor,
    OpDefinition,
    OpStep,
    _canonical_op,
    _deserialize_op,
    _serialize_op,
)
from .runner import (  # noqa: F401 - re-exported for callers that import them from here
    MESSAGE_MAX_CHARS,
    STEP_FUNCTIONS,
    LifecycleRunner,
    UsageUnavailable,
    _key_component,
)
from .states import RunState
from .track import PAYLOAD_INLINE_MAX_BYTES, TrackEvent, TrackStore


class WorkflowEngine(Protocol):
    """Experimental boundary used by callers, independent of engine choice."""

    def submit(self, op: OpDefinition) -> RunState:
        """Start or recover an Op."""

    def open_escalation(self, run_id: str) -> Mapping[str, Any] | None:
        """What a run waiting at a Gate waits on, or ``None``: what ``decide`` answers."""

    def decide(
        self, run_id: str, *, escalation: str | None, actor: str, outcome: str,
        findings: Sequence[Any] | None = (),
    ) -> RunState:
        """Answer the escalation a run waits on: approve, reject or send back."""

    def observe(self, run_id: str) -> RunState | None:
        """Return the run's state reconstructed from the Track; ``None`` if never submitted."""


class DurableWorkflowEngine(WorkflowEngine):
    """An engine whose recovery source is exclusively the Track.

    Experimental: interfaces may change. submit(), decide(), and retry() run
    synchronously until the run completes, fails, or waits at a Gate. After a process restart,
    a caller must resubmit the same Op; no background recovery loop is provided.

    Single-owner by assumption: it holds no cross-replica lease, so the same run
    must not be advanced from two API replicas concurrently. Multi-replica
    single-owner execution (an advancement lease) is provided by the crash-safe
    engine backing tracked in #1; the Postgres Track's one-submission-per-run index
    guards only a duplicated *submission*, not concurrent *advancement*.

    It makes no lifecycle decision itself: every method delegates to its
    ``LifecycleRunner``, where each change of a run's or a worker's state is a
    transition of its machine (``states/``) whose records the runner writes.
    """

    def __init__(
        self,
        *,
        executor: CogExecutor,
        track: TrackStore,
        budget: RunBudget | None = None,
        max_revisions: int | None = None,
        payload_inline_max_bytes: int = PAYLOAD_INLINE_MAX_BYTES,
    ) -> None:
        self.runner = LifecycleRunner(
            executor=executor, track=track, budget=budget, max_revisions=max_revisions,
            payload_inline_max_bytes=payload_inline_max_bytes,
        )

    # The runner holds the configuration; the engine exposes it where callers read it.

    @property
    def executor(self) -> CogExecutor:
        return self.runner.executor

    @property
    def track(self) -> TrackStore:
        return self.runner.track

    @property
    def budget(self) -> RunBudget | None:
        return self.runner.budget

    @property
    def max_revisions(self) -> int | None:
        return self.runner.max_revisions

    @property
    def payload_inline_max_bytes(self) -> int:
        return self.runner.payload_inline_max_bytes

    def submit(self, op: OpDefinition) -> RunState:
        return self.runner.submit(op)

    def retry(self, run_id: str) -> RunState:
        """Re-drive an unsuccessfully-ended run from its first incomplete step (``LifecycleRunner.retry``)."""
        return self.runner.retry(run_id)

    def decide(
        self, run_id: str, *, escalation: str | None, actor: str, outcome: str,
        findings: Sequence[Any] | None = (),
    ) -> RunState:
        """Answer the escalation a run waits on (``LifecycleRunner.decide``)."""
        return self.runner.decide(run_id, escalation=escalation, actor=actor, outcome=outcome, findings=findings)

    def observe(self, run_id: str) -> RunState | None:
        return self.runner.observe(run_id)

    def open_escalation(self, run_id: str) -> Mapping[str, Any] | None:
        """What a run waiting at a Gate waits on — the escalation id, the envelope, who may decide — or ``None``."""
        return self.runner.open_escalation(run_id)

    def _budget_tracker(self, events: tuple[TrackEvent, ...]) -> BudgetTracker | None:
        """The run's budget, reconstructed from its Track (``LifecycleRunner.budget_tracker``)."""
        return self.runner.budget_tracker(events)
