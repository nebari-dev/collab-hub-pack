"""Execution contracts shared by the Hub's Cog and Op implementations.

Experimental: Python interfaces, the worker protocol, and event schemas are
subject to breaking changes. See execution/README.md for current limitations.

Guarantees and non-goals
------------------------
``DurableWorkflowEngine`` is a single-owner, at-least-once reference engine. Its
durability is Track-based recovery, not distributed ownership:

- **Single-owner.** It holds no cross-replica lease, so one run must be advanced
  by one owner at a time. The Postgres Track's one-submission-per-run index guards
  a duplicate *submission*, not two callers concurrently *advancing* the same run.
- **At-least-once.** The engine keeps a stable idempotency key across a
  crash-recovery resume and passes it to the worker, but the reference and
  Kubernetes workers do not persist keys, so a replaced worker re-runs the side
  effect. Terminal runs are immutable; failed runs can be retried explicitly.
  Runs that exhausted a duration/token/cost budget require a new run id.
- **Caller-driven recovery.** submit(), signal(), and retry() are synchronous.
  After a process restart, a caller must resubmit the same incomplete Op. This
  package provides no startup reconciliation or background recovery loop.

Do not use this engine for multi-replica production execution until the
crash-safe engine backing tracked in collab-hub-pack #1 supplies ownership leases
and durable keyed (exactly-once) claims. ``WorkflowEngine`` and the Track/executor
interfaces separate these concerns, but their shapes are still experimental.
"""

from .binding import (
    BindingResolutionError,
    CapabilityRequirement,
    ContextCog,
    DeclaredCapabilityResolver,
    ModelBinding,
    ModelCog,
)
from .kubernetes import KubernetesCogExecutor, cog_slug, label_value, resource_name
from .lifecycle import (
    BudgetExceeded,
    BudgetTracker,
    CogLifecycle,
    LifecycleState,
    RunBudget,
)
from .orchestration import (
    DurableWorkflowEngine,
    InMemoryCogExecutor,
    OpDefinition,
    OpStep,
    PauseRequest,
    WorkflowEngine,
)
from .track import (
    InMemoryTrackStore,
    PostgresTrackStore,
    RunStatus,
    TrackEvent,
    TrackStore,
    derive_run_status,
)

__all__ = [
    "BindingResolutionError",
    "CapabilityRequirement",
    "ContextCog",
    "DeclaredCapabilityResolver",
    "BudgetExceeded",
    "BudgetTracker",
    "CogLifecycle",
    "InMemoryTrackStore",
    "LifecycleState",
    "PostgresTrackStore",
    "RunBudget",
    "RunStatus",
    "TrackEvent",
    "TrackStore",
    "derive_run_status",
    "ModelBinding",
    "ModelCog",
    "DurableWorkflowEngine",
    "InMemoryCogExecutor",
    "OpDefinition",
    "OpStep",
    "PauseRequest",
    "WorkflowEngine",
    "KubernetesCogExecutor",
    "cog_slug",
    "label_value",
    "resource_name",
]
