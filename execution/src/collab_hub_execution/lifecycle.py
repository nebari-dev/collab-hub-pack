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
    """State machine shared by one-shot and warm Cog workers."""

    def __init__(self) -> None:
        self.state = LifecycleState.MATERIALIZED

    def transition(self, target: LifecycleState) -> LifecycleState:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidLifecycleTransition(f"cannot transition {self.state} → {target}")
        self.state = target
        return self.state


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Hard per-run limits; ``None`` means that dimension is unbounded."""

    max_duration: timedelta | None = None
    max_tokens: int | None = None
    max_cost: float | None = None


class BudgetExceeded(RuntimeError):
    """Raised before an interaction would exceed a run budget."""


class BudgetTracker:
    def __init__(self, budget: RunBudget, *, started_at: datetime | None = None) -> None:
        self.budget = budget
        self.started_at = started_at or datetime.now(UTC)
        self.tokens = 0
        self.cost = 0.0

    def check(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self.budget.max_duration is not None and now >= self.started_at + self.budget.max_duration:
            raise BudgetExceeded("run duration budget exceeded")
        if self.budget.max_tokens is not None and self.tokens >= self.budget.max_tokens:
            raise BudgetExceeded("run token budget exceeded")
        if self.budget.max_cost is not None and self.cost >= self.budget.max_cost:
            raise BudgetExceeded("run cost budget exceeded")

    def consume(self, *, tokens: int = 0, cost: float = 0.0, now: datetime | None = None) -> None:
        self.tokens += tokens
        self.cost += cost
        self.check(now=now)
