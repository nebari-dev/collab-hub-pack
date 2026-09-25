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
from collections.abc import Iterable, Mapping
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
        if isinstance(self.approvers, (str, bytes)):
            raise ValueError("a Gate's approvers are a sequence of role names, not one string")
        # Read the sequence once: a generator read twice would validate and then be empty.
        approvers = tuple(self.approvers)
        if not all(isinstance(role, str) and role for role in approvers):
            raise ValueError("a Gate's approvers are a sequence of role names")
        object.__setattr__(self, "approvers", approvers)

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
        """The Gate a step was submitted with; a step recorded before Gates has the default one.

        A recorded Gate is read, never refused: a run must stay decidable even when
        its Track was written by an engine this one does not know — after a
        rollback, say. A policy this engine does not know reads as ``always``, the
        strictest, so a person decides every result rather than the run becoming
        undrivable; approvers that are not role names read as none declared, so the
        Gate's escalations fall to the default approvers instead of to roles nobody
        holds. A Gate a caller *declares* is still refused (``__post_init__``).
        """
        if value is None:
            return cls()
        policy = value.get("escalate", "error")
        recorded = value.get("approvers", ())
        if isinstance(recorded, (str, bytes)) or not isinstance(recorded, Iterable):
            recorded = ()
        approvers = tuple(role for role in recorded if isinstance(role, str) and role)
        return cls(escalate=policy if policy in POLICIES else "always", approvers=approvers)


def envelope_digest(envelope: ResultEnvelope | Mapping[str, Any]) -> str:
    """A stable id for one result: what a decision names as the envelope it decided on."""
    shape = envelope.to_dict() if isinstance(envelope, ResultEnvelope) else dict(envelope)
    digest = hashlib.sha256(json.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()
    return f"env-{digest[:16]}"


def escalation_id(run_id: str, step: str, attempt: int, envelope: ResultEnvelope) -> str:
    """The id of one escalation, minted over the step attempt and the envelope that escalated.

    A decision names it, so a decision is bound to the revision its reviewer saw:
    a send back re-runs the step, and the next escalation gets a new id.
    """
    material = {"run": run_id, "step": step, "attempt": attempt, "envelope": envelope.to_dict()}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    return f"esc-{digest[:16]}"
