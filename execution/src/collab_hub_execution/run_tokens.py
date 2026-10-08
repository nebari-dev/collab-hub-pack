"""The run token: what a worker presents to the hub, and the controller to the worker.

One per materialized worker. The controller mints it when it materializes the
worker; it reaches the worker in its environment at ``local`` and mounted into
the pod at ``remote``; it expires when the worker is torn down. It is an opaque
secret: only its hash is recorded, on the run's Track — the store the API and
the controller share — so either process checks a token without asking the
other, and the Track never holds a secret.

A worker presents it to every hub endpoint it calls (the claim transport, the
model egress gateway, the connector proxy), and the controller presents it to
the worker's ``/invoke`` as a bearer token, which a worker checks against the
one in its environment. One token, both directions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

RUN_TOKEN_ENV = "COLLAB_RUN_TOKEN"  # noqa: S105 - the variable's name, not a secret

# A run that ended, or whose host stopped, has no worker left: every token it issued has expired.
_ENDS_EVERY_WORKER = frozenset({"completed", "failed", "cancelled", "interrupted", "budget_exceeded"})


class _Fact(Protocol):
    event_type: str
    payload: Mapping[str, Any]


def mint() -> str:
    """A new run token."""
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    """What the Track records of a token: its sha256, never the token."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify(events: Iterable[_Fact], token: str) -> Mapping[str, Any] | None:
    """The worker a token belongs to on this run's Track, while that worker is up; ``None`` otherwise.

    The token of a worker that was torn down no longer verifies, nor does any
    token of a run that ended or whose host stopped, nor one this run never
    issued: a token reaches its own run only, and only while its worker is up.
    """
    presented = digest(token)
    live: dict[str, Mapping[str, Any]] = {}
    for event in events:
        instance = event.payload.get("instance")
        if event.event_type == "worker_started" and instance is not None:
            live[instance] = event.payload
        elif event.event_type == "worker_stopped":
            live.pop(instance, None)
        elif event.event_type in _ENDS_EVERY_WORKER:
            live.clear()
    for started in live.values():
        recorded = started.get("run_token_sha256")
        if isinstance(recorded, str) and hmac.compare_digest(recorded, presented):
            return started
    return None
