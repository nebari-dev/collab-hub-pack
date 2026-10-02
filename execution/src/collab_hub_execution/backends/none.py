"""The ``none`` durability backend: step functions run in process, and nothing is checkpointed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class NoneBackend:
    """Calls each step function directly, in the host's process.

    Nothing survives the host: a run it was advancing when the host stopped is
    recorded ``interrupted`` when a host next starts, and a person retries it.
    """

    name = "none"
    durable = False

    def run_step(self, run_id: str, name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return function(*args, **kwargs)
