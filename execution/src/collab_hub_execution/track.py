"""Durable, replayable Track events and status derivation.

A Track is the append-only record of one Op. Callers observe a run by reading
the Track; status is a projection of its events, never a separately maintained
mutable field.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4


class RunStatus(StrEnum):
    UNKNOWN = "unknown"
    SUBMITTED = "submitted"
    RUNNING = "running"
    MATERIALIZED = "materialized"
    READY = "ready"
    INTERACTING = "interacting"
    PAUSED = "paused"
    IDLE = "idle"
    TEARING_DOWN = "tearing_down"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True, slots=True)
class TrackEvent:
    """One immutable fact in a run's Track."""

    run_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: uuid4().hex)
    sequence: int | None = None


class TrackStore(Protocol):
    """Append, replay, and stream the durable history for an Op."""

    def append(self, event: TrackEvent) -> TrackEvent:
        """Append an event and return it with its assigned sequence."""

    def replay(self, run_id: str, *, after_sequence: int = 0) -> tuple[TrackEvent, ...]:
        """Replay events in sequence order."""

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        """Yield new events, optionally polling until the timeout expires."""


_STATUS_EVENTS = {
    "submitted": RunStatus.SUBMITTED,
    "step_started": RunStatus.RUNNING,
    "materialized": RunStatus.MATERIALIZED,
    "ready": RunStatus.READY,
    "interaction_started": RunStatus.INTERACTING,
    "paused": RunStatus.PAUSED,
    "idle": RunStatus.IDLE,
    "teardown_started": RunStatus.TEARING_DOWN,
    # A finished step returns the run to RUNNING (it's between steps / progressing),
    # not TEARING_DOWN — that per-step teardown is done. The final `completed`
    # overrides this on the last step.
    "step_completed": RunStatus.RUNNING,
    "completed": RunStatus.COMPLETED,
    "failed": RunStatus.FAILED,
    "timed_out": RunStatus.TIMED_OUT,
    "budget_exceeded": RunStatus.BUDGET_EXCEEDED,
}


def derive_run_status(events: Iterator[TrackEvent] | tuple[TrackEvent, ...]) -> RunStatus:
    """Calculate the status represented by the latest known event."""

    status = RunStatus.UNKNOWN
    for event in events:
        status = _STATUS_EVENTS.get(event.event_type, status)
    return status


class InMemoryTrackStore:
    """Thread-safe TrackStore used by tests and local development."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_sequence = 1
        self._events: dict[str, list[TrackEvent]] = defaultdict(list)

    def append(self, event: TrackEvent) -> TrackEvent:
        with self._lock:
            if event.sequence is not None:
                raise ValueError("TrackStore assigns event sequences")
            stored = TrackEvent(
                run_id=event.run_id,
                event_type=event.event_type,
                payload=dict(event.payload),
                occurred_at=event.occurred_at,
                event_id=event.event_id,
                sequence=self._next_sequence,
            )
            self._next_sequence += 1
            self._events[event.run_id].append(stored)
            return stored

    def replay(self, run_id: str, *, after_sequence: int = 0) -> tuple[TrackEvent, ...]:
        with self._lock:
            return tuple(event for event in self._events.get(run_id, ()) if (event.sequence or 0) > after_sequence)

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        deadline = time.monotonic() + timeout_seconds
        cursor = after_sequence
        while True:
            events = self.replay(run_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence or cursor
                yield event
            if not timeout_seconds or time.monotonic() >= deadline:
                return
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


class PostgresTrackStore:
    """TrackStore backed by a PostgreSQL connection pool.

    The pool is supplied by the application, so this adapter does not create
    connections or decide deployment topology. The table's global sequence
    gives each event a stable replay order, including events written by
    multiple API replicas.
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    # One statement per execute: psycopg3's extended protocol rejects multiple
    # commands in a single execute().
    _SCHEMA = (
        "CREATE SEQUENCE IF NOT EXISTS collab_track_event_sequence",
        """
        CREATE TABLE IF NOT EXISTS collab_track_events (
            sequence bigint PRIMARY KEY DEFAULT nextval('collab_track_event_sequence'),
            event_id text NOT NULL UNIQUE,
            run_id text NOT NULL,
            event_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_track_events_run_sequence ON collab_track_events (run_id, sequence)",
        # At most one submission per run: two API replicas cannot both start the
        # same run (the losing append raises). Single-owner lease / graceful
        # handling of the conflict lands with the crash-safe engine backing (#1).
        "CREATE UNIQUE INDEX IF NOT EXISTS collab_track_one_submission "
        "ON collab_track_events (run_id) WHERE event_type = 'op_submitted'",
    )

    @staticmethod
    def ensure_schema(connection: Any) -> None:
        """Create the Track table and indexes if absent (idempotent).

        The adapter never calls this itself: append()/replay() assume the table
        exists. Creating it is the application's responsibility — run this once at
        startup or as a migration step, before the store handles traffic.
        """
        for statement in PostgresTrackStore._SCHEMA:
            connection.execute(statement)

    def append(self, event: TrackEvent) -> TrackEvent:
        if event.sequence is not None:
            raise ValueError("TrackStore assigns event sequences")
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO collab_track_events
                    (event_id, run_id, event_type, payload, occurred_at)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                RETURNING sequence
                """,
                (event.event_id, event.run_id, event.event_type, json.dumps(event.payload), event.occurred_at),
            ).fetchone()
            return replace(event, sequence=row[0])

    def replay(self, run_id: str, *, after_sequence: int = 0) -> tuple[TrackEvent, ...]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_id, run_id, event_type, payload, occurred_at
                FROM collab_track_events
                WHERE run_id = %s AND sequence > %s
                ORDER BY sequence
                """,
                (run_id, after_sequence),
            ).fetchall()
        return tuple(
            TrackEvent(
                sequence=row[0],
                event_id=row[1],
                run_id=row[2],
                event_type=row[3],
                payload=row[4],
                occurred_at=row[5],
            )
            for row in rows
        )

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        deadline = time.monotonic() + timeout_seconds
        cursor = after_sequence
        while True:
            events = self.replay(run_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence or cursor
                yield event
            if not timeout_seconds or time.monotonic() >= deadline:
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
