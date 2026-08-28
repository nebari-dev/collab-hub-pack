"""Contracts and in-memory reference implementation for Cog execution.

This package is deliberately independent of FastAPI, Kubernetes, and any
particular workflow engine. Production adapters should implement the
protocols in :mod:`collab_hub_api.execution.contracts` rather than moving
orchestration into infrastructure-specific code.
"""

from .contracts import (
    CapabilityResolver,
    CogClass,
    CogDefinition,
    CogExecutor,
    CogWorker,
    ExecutionEvent,
    ExecutionOrchestrator,
    Op,
    RunSnapshot,
    RunStatus,
    TrackStore,
)
from .in_memory import (
    InMemoryExecutor,
    InMemoryOrchestrator,
    InMemoryResolver,
    InMemoryTrackStore,
    derive_run_status,
)

__all__ = [
    "CapabilityResolver",
    "CogClass",
    "CogDefinition",
    "CogExecutor",
    "CogWorker",
    "ExecutionEvent",
    "ExecutionOrchestrator",
    "InMemoryExecutor",
    "InMemoryOrchestrator",
    "InMemoryResolver",
    "InMemoryTrackStore",
    "Op",
    "RunSnapshot",
    "RunStatus",
    "TrackStore",
    "derive_run_status",
]
