"""Gates: the decision point on an Op step.

A Gate is declared on the step and evaluated by the engine over the step's
result envelope. A Cog never decides whether its own output needs a person;
it reports problems, and the step's Gate decides what they mean (ADR-0002 D8).
A Gate has three outcomes: ``pass`` (nothing to review), ``pass_with_problems``
(the problems are recorded and the run goes on), and ``escalate`` (the run waits
at the Gate until an approver approves, rejects or sends the step back).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .envelope import ResultEnvelope

# Organization owners and platform operators decide a Gate that names no approvers (decision 1).
DEFAULT_APPROVERS = ("owner", "operator")

# What a Gate escalates, from the least to the most: nothing, a problem with
# severity `error`, any problem, or every result (a sign-off).
POLICIES = ("never", "error", "warn", "always")


class GateOutcome(StrEnum):
    PASS = "pass"
    PASS_WITH_PROBLEMS = "pass_with_problems"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class Gate:
    """A step's Gate. The default escalates any problem with severity ``error``."""

    escalate: str = "error"
    approvers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.escalate not in POLICIES:
            raise ValueError(f"a Gate escalates one of {POLICIES}, not {self.escalate!r}")
        if isinstance(self.approvers, str) or not all(isinstance(r, str) and r for r in self.approvers):
            raise ValueError("a Gate's approvers are a sequence of role names")
        object.__setattr__(self, "approvers", tuple(self.approvers))

    @property
    def deciders(self) -> tuple[str, ...]:
        """The roles that may decide this Gate's escalations."""
        return self.approvers or DEFAULT_APPROVERS

    def evaluate(self, envelope: ResultEnvelope) -> tuple[GateOutcome, str | None]:
        """The Gate's outcome for an ``ok: true`` envelope, and why it escalates when it does."""
        severities = {problem.severity for problem in envelope.problems}
        if self.escalate == "always":
            return GateOutcome.ESCALATE, "the Gate asks an approver to sign off every result"
        if self.escalate == "error" and "error" in severities:
            return GateOutcome.ESCALATE, "a problem with severity error"
        if self.escalate == "warn" and severities:
            return GateOutcome.ESCALATE, f"a problem with severity {'error' if 'error' in severities else 'warn'}"
        return (GateOutcome.PASS_WITH_PROBLEMS if envelope.problems else GateOutcome.PASS), None

    def to_dict(self) -> dict[str, Any]:
        return {"escalate": self.escalate, "approvers": list(self.approvers)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> Gate:
        """The Gate a step was submitted with; a step recorded before Gates has the default one."""
        if value is None:
            return cls()
        return cls(escalate=value.get("escalate", "error"), approvers=tuple(value.get("approvers", ())))


def escalation_id(run_id: str, step: str, attempt: int, envelope: ResultEnvelope) -> str:
    """The id of one escalation, minted over the step attempt and the envelope that escalated.

    A decision names it, so a decision is bound to the revision its reviewer saw:
    a send back re-runs the step, and the next escalation gets a new id.
    """
    material = {"run": run_id, "step": step, "attempt": attempt, "envelope": envelope.to_dict()}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    return f"esc-{digest[:16]}"
