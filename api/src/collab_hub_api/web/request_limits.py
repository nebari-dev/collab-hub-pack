"""Bounding what a request body may cost, for every route family that reads one.

Lifted out of ``routers.invite`` (issue #90) when the operator page (#91) was
found to have the same defect the acceptance page had already fixed: a size cap
decided from ``Content-Length`` alone, which is **no cap at all** against a
chunked request, because a chunked request carries no such header. Two
independent implementations of one bound is how the second one gets it wrong,
and it is why this lives here instead of being copied a third time.

The rule, stated once
---------------------
``Content-Length`` is a **fast path and never the gate**. It is absent on a
chunked request and can understate the real body on any request, so a limit
that consults only the header is not a limit — it is a request that the caller
behave. :func:`bounded_body` counts what actually arrives and stops at the
first chunk that crosses the cap, which holds for chunked and non-chunked
alike and for a ``Content-Length`` that lies in either direction.

Refusing costs a connection unless you close it
-----------------------------------------------
Answering an HTTP/1.1 request whose body has **not** been read to
end-of-message leaves the server unable to start the next cycle on that
connection: it buffers what the client is still sending up to its high-water
mark and then stalls, holding the connection until something times out. On the
oversize path that is the exact failure being defended against, moved from
memory to connections — a caller who keeps sending ties up a connection per
request, and the size cap that stopped the memory problem is what creates it.

So a refusal issued before the body was read must close the connection, which
is what a 413 conventionally does anyway. Draining instead would mean reading
past the cap, which is the original problem again.

:func:`connection_close_headers` is the one spelling of that, and every caller
in this codebase passes ``body_consumed`` **explicitly, with no default** — see
``routers.invite._outcome_response`` and ``routers.admin._page``. The safe value
depends on where the call sits, and a refusal path added later above the read
would silently inherit the wrong one; being made to answer the question is the
point.
"""

from __future__ import annotations

from fastapi import Request

__all__ = ["bounded_body", "connection_close_headers", "declares_oversize"]


def declares_oversize(request: Request, *, max_bytes: int) -> bool:
    """A fast path only: refuse a body the caller *admits* is too big.

    Never the gate — see the module docstring. A malformed ``Content-Length``
    is treated as oversize rather than ignored: a header the server cannot
    parse is one it cannot use to decide anything, and the safe reading of an
    undecidable declaration is refusal.
    """

    declared = request.headers.get("content-length")
    if declared is None:
        return False
    try:
        return int(declared) > max_bytes
    except ValueError:
        return True


async def bounded_body(request: Request, *, max_bytes: int) -> bytes | None:
    """Read the body, or ``None`` the moment it exceeds *max_bytes*.

    Streamed and counted rather than buffered, because ``request.body()`` and
    ``request.form()`` both read to completion with no bound of their own.
    Iteration stops at the first chunk that crosses the cap; **nothing after it
    is read**, which is the point — the caller then answers with
    ``connection_close_headers(body_consumed=False)``.

    ``None`` rather than a raise, so the decision is a branch at the call site
    rather than an exception that some enclosing handler might swallow into a
    500 and, in doing so, read the rest of the body on the way out.
    """

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def connection_close_headers(*, body_consumed: bool) -> dict[str, str]:
    """``{"Connection": "close"}`` when the body was not read, else ``{}``.

    Keyword-only and undefaulted at every call site for the reason in the
    module docstring: the correct value is a property of *where the response is
    issued*, not of the response itself.
    """

    return {} if body_consumed else {"Connection": "close"}
