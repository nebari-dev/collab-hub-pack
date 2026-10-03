"""The request budget, carried into blocking store calls (issue #179).

A ``/v2/`` request runs under one aggregate deadline, but its catalog and
credential lookups are synchronous psycopg calls on a worker thread, and
cancelling the coroutine that awaits a worker does not stop the worker: a
query the database is slow to answer would keep its thread and its pooled
connection after the request had already answered 503.

So the budget is handed to the database. The router records the request's
deadline in :data:`request_deadline` (a context variable, which the
threadpool hop copies), and every serving store call checks its connection
out through :func:`bounded_connection`: the pool wait is capped at what is
left, and a transaction-local ``statement_timeout`` makes the *server* abort
a statement that outlives it. The worker then unwinds, the transaction rolls
back, and the connection goes back to the pool.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_BUDGET_SECONDS = 10.0
"""Budget for a store call made outside a ``/v2/`` request (the credential exchange, the token endpoint)."""

request_deadline: ContextVar[float | None] = ContextVar("cog_serving_request_deadline", default=None)
"""``time.monotonic()`` at which the current request's budget runs out, or ``None`` outside one."""


class BudgetExhausted(TimeoutError):
    """The request's budget was already spent before a store call could start."""


def remaining_seconds() -> float:
    """What is left of the current request's budget; the default budget when there is no request."""

    deadline = request_deadline.get()
    if deadline is None:
        return DEFAULT_BUDGET_SECONDS
    return deadline - time.monotonic()


@contextmanager
def bounded_connection(db):
    """A pooled connection whose acquisition and statements cannot outlive the request budget.

    Raises :class:`BudgetExhausted` without touching the pool when nothing is
    left. Otherwise the checkout waits at most the remaining budget (psycopg's
    ``PoolTimeout`` past it), and every statement in the transaction is
    bounded by a transaction-local ``statement_timeout`` (``QueryCanceled``
    past it) -- local, so the connection returns to the pool unaltered.
    """

    remaining = remaining_seconds()
    if remaining <= 0:
        raise BudgetExhausted("the request's time budget is spent")
    with db.connection(timeout=remaining) as conn:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        yield conn
