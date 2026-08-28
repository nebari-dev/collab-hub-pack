"""Small in-memory adapters used as the execution walking skeleton."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from .contracts import (
    CapabilityResolver,
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


class CapabilityUnavailableError(LookupError):
    """Raised when no Cog provides all required capabilities."""


class InvalidEntryPointError(ValueError):
    """Raised when an Op names an entry point the resolved Cog did not declare."""


def derive_run_status(events: Iterable[ExecutionEvent]) -> RunStatus:
    """Derive status solely from the latest event in a Track."""

    status_by_event = {
        "submitted": RunStatus.SUBMITTED,
        "materialized": RunStatus.MATERIALIZED,
        "ready": RunStatus.READY,
        "interaction_started": RunStatus.INTERACTING,
        "paused": RunStatus.PAUSED,
        "idle": RunStatus.IDLE,
        "completed": RunStatus.COMPLETED,
        "failed": RunStatus.FAILED,
    }
    status = RunStatus.UNKNOWN
    for event in events:
        status = status_by_event.get(event.kind, status)
    return status


class InMemoryTrackStore(TrackStore):
    """Append-only Track implementation for tests and local development."""

    def __init__(self) -> None:
        self._events: dict[str, list[ExecutionEvent]] = defaultdict(list)

    def append(self, event: ExecutionEvent) -> None:
        self._events[event.run_id].append(event)

    def replay(self, run_id: str) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events.get(run_id, ()))

    def stream(self, run_id: str) -> Iterator[ExecutionEvent]:
        yield from self.replay(run_id)


class InMemoryResolver(CapabilityResolver):
    """Select a Cog whose provided capabilities and dependencies are available."""

    def resolve(self, required: Iterable[str], candidates: Sequence[CogDefinition]) -> CogDefinition:
        required_set = frozenset(required)
        available = frozenset(capability for candidate in candidates for capability in candidate.provides)
        for candidate in candidates:
            if required_set <= candidate.provides and candidate.requires <= available:
                return candidate
        raise CapabilityUnavailableError(f"no Cog provides required capabilities: {', '.join(sorted(required_set))}")


class _InMemoryWorker(CogWorker):
    def __init__(self, cog: CogDefinition, handler: Callable[[str, Any], Any]) -> None:
        self.cog = cog
        self.handler = handler

    def interact(self, entry_point: str, input: Any = None) -> Any:
        if entry_point not in self.cog.entry_points:
            raise InvalidEntryPointError(f"Cog {self.cog.name!r} has no entry point {entry_point!r}")
        return self.handler(entry_point, input)


class InMemoryExecutor(CogExecutor):
    """Executor that records lifecycle calls and delegates interaction to fakes."""

    def __init__(self, handlers: dict[str, Callable[[str, Any], Any]]) -> None:
        self.handlers = handlers
        self.materialized: list[tuple[str, str]] = []
        self.torn_down: list[str] = []

    def materialize(self, cog: CogDefinition, run_id: str) -> CogWorker:
        try:
            handler = self.handlers[cog.name]
        except KeyError as exc:
            raise LookupError(f"no in-memory handler registered for Cog {cog.name!r}") from exc
        self.materialized.append((run_id, cog.name))
        return _InMemoryWorker(cog, handler)

    def teardown(self, worker: CogWorker) -> None:
        self.torn_down.append(worker.cog.name)  # type: ignore[attr-defined]


class InMemoryOrchestrator(ExecutionOrchestrator):
    """Walking skeleton: resolve → materialize → interact → observe → teardown."""

    def __init__(
        self,
        *,
        executor: CogExecutor,
        resolver: CapabilityResolver,
        track: TrackStore,
        candidates: Sequence[CogDefinition],
    ) -> None:
        self.executor = executor
        self.resolver = resolver
        self.track = track
        self.candidates = candidates
        self._workers: dict[str, CogWorker] = {}

    def _append(self, run_id: str, kind: str, **data: Any) -> None:
        self.track.append(ExecutionEvent(run_id=run_id, kind=kind, data=data))

    def observe(self, run_id: str) -> RunSnapshot:
        events = self.track.replay(run_id)
        return RunSnapshot(run_id=run_id, status=derive_run_status(events), events=events)

    def submit(self, op: Op) -> RunSnapshot:
        self._append(op.run_id, "submitted")
        cog = self.resolver.resolve(op.required_capabilities, self.candidates)
        worker = self.executor.materialize(cog, op.run_id)
        self._workers[op.run_id] = worker
        self._append(op.run_id, "materialized", cog=cog.name)
        self._append(op.run_id, "ready", cog=cog.name)
        self._append(op.run_id, "interaction_started", entry_point=op.entry_point)
        try:
            worker.interact(op.entry_point, op.input)
        except Exception as exc:
            self._append(op.run_id, "failed", error=type(exc).__name__)
            self.executor.teardown(worker)
            self._workers.pop(op.run_id, None)
            raise
        self._append(op.run_id, "idle")
        self.executor.teardown(worker)
        self._workers.pop(op.run_id, None)
        self._append(op.run_id, "completed")
        return self.observe(op.run_id)

    def signal(self, run_id: str, signal: str, value: Any = None) -> RunSnapshot:
        """Record a signal boundary without inventing engine-specific behavior."""

        if run_id not in self._workers:
            raise LookupError(f"no active worker for run {run_id!r}")
        worker = self._workers[run_id]
        self._append(run_id, "interaction_started", signal=signal)
        worker.interact(signal, value)
        self._append(run_id, "idle")
        return self.observe(run_id)
