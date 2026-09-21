"""Per-run budget rules.

A worker's lifecycle states are the worker machine in ``states/worker.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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

    Configured token/cost limits require corresponding usage from every worker
    interaction, including a pause. Unknown or invalid usage fails accounting;
    it is never counted as zero for a configured limit.
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
