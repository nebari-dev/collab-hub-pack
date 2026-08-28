"""Stable boundaries for running Ops through Cogs.

A Cog is a self-running worker with declared entry points. An Op is a run
composed from one or more Cogs. The interfaces here describe how an Op is
resolved, materialized, interacted with, and observed without choosing where
the worker lives or which engine coordinates it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class CogClass(StrEnum):
    """The declared role of a Cog; these are data, not separate subclasses."""

    MODEL = "model"
    HARNESS = "harness"
    CONTEXT = "context"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class CogDefinition:
    """The declared data needed to resolve and interact with a Cog."""

    name: str
    cog_class: CogClass
    entry_points: tuple[str, ...]
    provides: frozenset[str] = field(default_factory=frozenset)
    requires: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Op:
    """A request to run one entry point on a resolved Cog."""

    run_id: str
    required_capabilities: frozenset[str]
    entry_point: str
    input: Any = None


class RunStatus(StrEnum):
    """Status derived from the events in a run's Track."""

    UNKNOWN = "unknown"
    SUBMITTED = "submitted"
    MATERIALIZED = "materialized"
    READY = "ready"
    INTERACTING = "interacting"
    PAUSED = "paused"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One append-only fact about an Op."""

    run_id: str
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """A read model calculated from a run's Track."""

    run_id: str
    status: RunStatus
    events: tuple[ExecutionEvent, ...]


class CogWorker(Protocol):
    """A materialized Cog reached through its declared entry points."""

    def interact(self, entry_point: str, input: Any = None) -> Any:
        """Interact with an entry point; never expose opaque execution."""


class CogExecutor(Protocol):
    """Materializes and tears down workers; it owns placement-specific code."""

    def materialize(self, cog: CogDefinition, run_id: str) -> CogWorker:
        """Create a worker for a Cog and a single Op run."""

    def teardown(self, worker: CogWorker) -> None:
        """Release the resources belonging to a materialized worker."""


class CapabilityResolver(Protocol):
    """Resolves ``requires`` against ``provides`` and nothing else."""

    def resolve(
        self,
        required: Iterable[str],
        candidates: Sequence[CogDefinition],
    ) -> CogDefinition:
        """Return a candidate providing every required capability."""


class TrackStore(Protocol):
    """Stores the append-only history from which run status is derived."""

    def append(self, event: ExecutionEvent) -> None:
        """Append one event to a run's Track."""

    def replay(self, run_id: str) -> tuple[ExecutionEvent, ...]:
        """Replay all events for a run in append order."""

    def stream(self, run_id: str) -> Iterator[ExecutionEvent]:
        """Yield the current event history for a run."""


class ExecutionOrchestrator(Protocol):
    """Coordinates Ops without knowing worker placement or storage details."""

    def submit(self, op: Op) -> RunSnapshot:
        """Start an Op and return its Track-derived snapshot."""

    def signal(self, run_id: str, signal: str, value: Any = None) -> RunSnapshot:
        """Deliver a signal to a paused or active Op."""

    def observe(self, run_id: str) -> RunSnapshot:
        """Read the current state derived from the Track."""
