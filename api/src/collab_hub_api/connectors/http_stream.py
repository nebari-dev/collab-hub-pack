from __future__ import annotations

import httpx


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Stream a response body, stopping once it exceeds ``max_bytes``.

    Shared memory-exhaustion guard for connectors that read untrusted, potentially
    unbounded upstreams (a GitHub ``git/trees?recursive=1``, a large Drive export):
    the body is never fully buffered. ``aiter_bytes`` yields *decoded* chunks, so a
    single chunk from a highly-compressible (gzip-bombed) body can be tens of MB —
    each chunk is sliced to what still fits the ``max_bytes + 1`` sentinel BEFORE it
    is retained, so one giant chunk can't blow the bound. Returns ``(bytes,
    truncated)`` where ``truncated`` is True iff the upstream carried more than
    ``max_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        # `total <= max_bytes` on entry (we break the instant it exceeds), so the
        # keep-count is always >= 1; slice first, then count the full chunk length
        # so the cap check still detects overflow.
        chunks.append(chunk[: max_bytes + 1 - total])
        total += len(chunk)
        if total > max_bytes:
            truncated = True
            break
    return b"".join(chunks)[:max_bytes], truncated
