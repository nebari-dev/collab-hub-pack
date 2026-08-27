"""Off-request-path dispatch for database writes the caller can afford to lose.

Bounding *connection acquisition* is not enough to keep accounting off the
request path. Once a best-effort write has a connection, the statement itself
runs on the calling thread, and a database that accepts the statement and takes
its time answering holds that thread for as long as it likes — which for the
seen-user write means holding an authenticated HTTP request, or blocking the
event loop, since MCP calls the synchronous authenticator directly from async
middleware.

So the request path does not run these writes at all. It hands them to this
writer, which either queues the work or drops it, and returns. What that costs
is bounded on every axis:

- **Dispatch** is a ``put_nowait`` onto a bounded queue. A full queue drops the
  write rather than waiting for room: this is accounting, and the caller is a
  request.
- **Concurrency** is a fixed, small number of daemon threads. A database that
  never answers can park them — that is the residual an in-process client
  cannot escape — but it parks *them*, not requests, and it can never park more
  than this many threads or hold more than this many pooled connections.
  Everything behind them drops and is retried later under the caller's backoff.
- **Shutdown** waits a bounded time for work in progress and abandons the rest.
  The threads are daemons, so a parked one can never hold up process exit.

The database-side half of the deadline (a transaction-local
``statement_timeout``, and a cancel if the server sits past it) lives with the
checkout in :mod:`.db`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger("frames_server.usage")

# Two threads: enough that one slow write does not stall the next, small enough
# that a database which never answers parks a knowable, tiny share of the pool.
BEST_EFFORT_WORKERS = 2

# Room for a burst of distinct callers while the writers are busy. Reached only
# when writes are arriving faster than they complete, which for throttled
# accounting means the database is not answering — in which case dropping is
# the correct outcome, not queueing further.
BEST_EFFORT_QUEUE_SIZE = 100

BEST_EFFORT_SHUTDOWN_TIMEOUT_SECONDS = 2.0

_STOP = object()


class BestEffortWriter:
    """A bounded pool of daemon threads running writes off the request path.

    Workers start on the first submission, so building an app or a test fixture
    never spawns threads for accounting that may never happen.
    """

    def __init__(
        self,
        workers: int = BEST_EFFORT_WORKERS,
        queue_size: int = BEST_EFFORT_QUEUE_SIZE,
        name: str = "best-effort-write",
    ):
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._worker_count = workers
        self._name = name
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending = 0
        self._closed = False
        self.dropped = 0
        """Writes never attempted because the queue was full or the writer closed."""

    def submit(self, work) -> bool:
        """Queue *work* to run on a worker thread.

        Returns whether it was accepted. Never blocks and never raises: the
        caller is a request, and this is the whole point of the class.
        """

        with self._lock:
            if self._closed:
                self.dropped += 1
                return False
            self._start_workers()
            try:
                self._queue.put_nowait(work)
            except queue.Full:
                self.dropped += 1
                return False
            self._pending += 1
            return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until queued and in-flight work has drained, or *timeout*."""

        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)

    def close(self, timeout: float = BEST_EFFORT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Stop accepting work and wait a bounded time for what is running.

        Work still queued is dropped rather than drained: it is accounting, not
        state, and shutdown should not wait on a database that may be why the
        queue is long in the first place.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = list(self._threads)
            abandoned = 0
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                abandoned += 1
            self.dropped += abandoned
            self._pending -= abandoned
            for _ in threads:
                self._queue.put_nowait(_STOP)
            if self._pending == 0:
                self._idle.notify_all()

        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _start_workers(self) -> None:
        """Spawn the worker threads. Caller holds the lock."""

        if self._threads:
            return
        for index in range(self._worker_count):
            thread = threading.Thread(target=self._run, name=f"{self._name}-{index}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            if work is _STOP:
                return
            try:
                work()
            except Exception:
                # Work is expected to handle its own failures; anything landing
                # here is a bug, and it must not take the worker down with it.
                logger.exception("best_effort_write_crashed")
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()


class InlineBestEffortWriter(BestEffortWriter):
    """A writer that runs work on the calling thread.

    For tests that need a best-effort write to have finished by the time
    ``submit`` returns; never used on a request path.
    """

    def __init__(self):
        super().__init__(workers=0)

    def submit(self, work) -> bool:
        if self._closed:
            self.dropped += 1
            return False
        work()
        return True

    def close(self, timeout: float = BEST_EFFORT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        self._closed = True
