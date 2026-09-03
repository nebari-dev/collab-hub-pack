"""Shared Cog lifecycle and per-run budget rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class LifecycleState(StrEnum):
    MATERIALIZED = "materialized"
    READY = "ready"
    INTERACTING = "interacting"
    IDLE = "idle"
    TEARING_DOWN = "tearing_down"
    TORN_DOWN = "torn_down"
    FAILED = "failed"


class InvalidLifecycleTransition(ValueError):
    """Raised when a Cog skips or reverses a lifecycle transition."""


_TRANSITIONS = {
    LifecycleState.MATERIALIZED: {LifecycleState.READY, LifecycleState.FAILED},
    LifecycleState.READY: {LifecycleState.INTERACTING, LifecycleState.TEARING_DOWN, LifecycleState.FAILED},
    LifecycleState.INTERACTING: {LifecycleState.IDLE, LifecycleState.FAILED},
    LifecycleState.IDLE: {LifecycleState.INTERACTING, LifecycleState.TEARING_DOWN, LifecycleState.FAILED},
    LifecycleState.TEARING_DOWN: {LifecycleState.TORN_DOWN, LifecycleState.FAILED},
    LifecycleState.TORN_DOWN: set(),
    LifecycleState.FAILED: set(),
}


class CogLifecycle:
    """Local, per-interaction validation that a worker's states progress legally.

    This is NOT the durable state machine. It is created fresh for each step and
    only guards transition ordering within that step; it is never rehydrated. The
    Track is the durable record of a run — recovery derives state from Track events
    (materialized, ready, interaction_started, idle, step_completed, failed,
    teardown_failed), not from this object. Transitions here that the Track does
    not mirror (e.g. TORN_DOWN) are local assertions only.
    """

    def __init__(self) -> None:
        self.state = LifecycleState.MATERIALIZED

    def transition(self, target: LifecycleState) -> LifecycleState:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidLifecycleTransition(f"cannot transition {self.state} → {target}")
        self.state = target
        return self.state


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Per-run limits; ``None`` means that dimension is unbounded.

    Enforcement differs by dimension because of what is knowable before a step:

    - ``max_duration`` is a hard pre-check — elapsed time is known, so a step is
      not started once the deadline has passed.
    - ``max_tokens`` / ``max_cost`` are post-interaction accounting limits, not
      hard per-interaction caps. Usage is only known after the Cog runs, so a step
      started under budget can overshoot, and the run stops at the *next* boundary
      once cumulative usage crosses the limit. A hard per-request token cap is the
      model gateway's job (its ``max_tokens``); wiring that enforcement into the
      executor rollout is tracked in collab-hub-pack #1.
    """

    max_duration: timedelta | None = None
    max_tokens: int | None = None
    max_cost: float | None = None


class BudgetExceeded(RuntimeError):
    """Raised before an interaction would exceed a run budget.

    ``dimension`` is which limit was hit ("duration", "tokens", or "cost") so the
    engine can distinguish a timeout from overspend.
    """

    def __init__(self, message: str, *, dimension: str) -> None:
        super().__init__(message)
        self.dimension = dimension


class BudgetTracker:
    def __init__(self, budget: RunBudget, *, started_at: datetime | None = None) -> None:
        self.budget = budget
        self.started_at = started_at or datetime.now(UTC)
        self.tokens = 0
        self.cost = 0.0

    def check(self, *, now: datetime | None = None) -> None:
        # Called before a step (against elapsed time and cumulative usage so far)
        # and again after consume(). It does not bound a single interaction's spend
        # — that overshoots by design; see RunBudget for what each limit guarantees.
        # Limits are inclusive: reaching exactly max_tokens/max_cost, or the
        # deadline, stops the run (the limit is a hard ceiling, not a threshold to
        # pass).
        now = now or datetime.now(UTC)
        if self.budget.max_duration is not None and now >= self.started_at + self.budget.max_duration:
            raise BudgetExceeded("run duration budget exceeded", dimension="duration")
        if self.budget.max_tokens is not None and self.tokens >= self.budget.max_tokens:
            raise BudgetExceeded("run token budget exceeded", dimension="tokens")
        if self.budget.max_cost is not None and self.cost >= self.budget.max_cost:
            raise BudgetExceeded("run cost budget exceeded", dimension="cost")

    def consume(self, *, tokens: int = 0, cost: float = 0.0, now: datetime | None = None) -> None:
        self.tokens += tokens
        self.cost += cost
        self.check(now=now)
