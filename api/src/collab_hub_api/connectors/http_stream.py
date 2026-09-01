from __future__ import annotations

import httpx


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Stream a response body, stopping once it exceeds ``max_bytes``.

    Shared memory-exhaustion guard for connectors that read untrusted, potentially
    unbounded upstreams (a GitHub ``git/trees?recursive=1``, a large Drive export):
    the body is never fully buffered — the loop aborts one chunk past the cap and
    the final slice trims the overshoot. Returns ``(bytes, truncated)`` where
    ``truncated`` is True iff the upstream carried more than ``max_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            truncated = True
            break
    return b"".join(chunks)[:max_bytes], truncated
