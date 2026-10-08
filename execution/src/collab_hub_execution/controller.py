"""The run controller: the process that advances runs, separate from the one that accepts them.

    python -m collab_hub_execution.controller --track FILE --packages DIR [--packages DIR ...] --work-dir DIR

ADR-0002 D4. The API writes intent to the Track (``intents.py``); the
controller watches the Track and acts on it. A run submitted and not yet picked
up is started on the lifecycle runner, and a request to cancel is delivered to
the runner, which tears the run's worker down and ends it ``cancelled``.
Nothing calls the controller, and it alone constructs an executor.

This is the controller's first form, enough for one host: it polls the Track,
and it assumes it is the only controller on it — it holds a lock beside a
SQLite Track for as long as it runs, and a second one refuses to start. When it
starts, every run a previous controller left unfinished is recorded
``interrupted`` (``none`` keeps nothing across a restart). Pickup that two
replicas can race for, reaping a survivor by its recorded pid, and health
endpoints are the run controller's own phase of the plan.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import intents
from .locations import AGENT_LOCATIONS
from .runner import LifecycleRunner
from .states import InvalidTransition, RunState
from .track import SqliteTrackStore

_log = logging.getLogger("collab_hub_execution.controller")


class RunController:
    """Watches a Track and advances what it finds there, on one lifecycle runner."""

    def __init__(self, runner: LifecycleRunner, *, poll_interval: float = 0.25, session_grace: float = 30.0) -> None:
        self.runner = runner
        self.poll_interval = poll_interval
        self.session_grace = session_grace
        """How long a worker that was invoked may answer a turn "no session" before the turn fails."""
        self._ended: set[str] = set()
        self._advancing: dict[str, threading.Thread] = {}
        self._cancelling: dict[str, threading.Thread] = {}
        self._turning: dict[str, threading.Thread] = {}
        self._first_refused: dict[str, float] = {}  # turn -> when its worker first said no session was open
        self.views = intents.RunViews(runner.track)

    def start(self) -> tuple[str, ...]:
        """What a controller does first: record every run a stopped one left unfinished as interrupted."""
        interrupted = self.runner.start()
        for run_id in interrupted:
            _log.info("interrupted %s: a previous controller stopped before it ended", run_id)
        return interrupted

    def tick(self) -> None:
        """One pass over the Track: start what was submitted, deliver what was asked.

        Each run is read incrementally (``RunViews``), and each is handled on its
        own: a run whose Track cannot be read is logged and passed over, never
        stopping the pass for the runs after it.
        """
        for run_id in self.runner.track.run_ids():
            if run_id in self._ended:
                continue
            try:
                self._look_at(run_id)
            except intents.RunUnreadable:
                continue  # RunViews logged it once; it is read again only when its Track moves
            except Exception:  # noqa: BLE001 - one run's trouble never stops the pass
                _log.exception("handling %s failed; the next pass tries again", run_id)

    def _look_at(self, run_id: str) -> None:
        view = self.views.view(run_id)
        if view is None:
            return
        if view.state.ended:
            self._ended.add(run_id)
            self.views.forget(run_id)  # never read again here: keep nothing of it
            return
        if view.cancel_requested_by is not None:
            if not self._alive(self._cancelling, run_id):
                self._spawn(self._cancelling, run_id, self._cancel, run_id, view.cancel_requested_by)
        elif view.state is RunState.SUBMITTED and not self._alive(self._advancing, run_id):
            self._spawn(self._advancing, run_id, self._advance, view.op)
        if (any(turn.state == "pending" for turn in self.views.turns(run_id).values())
                and not self._alive(self._turning, run_id)):
            self._spawn(self._turning, run_id, self._deliver_turns, run_id)

    @staticmethod
    def _alive(threads: dict[str, threading.Thread], run_id: str) -> bool:
        thread = threads.get(run_id)
        return thread is not None and thread.is_alive()

    @staticmethod
    def _spawn(threads: dict[str, threading.Thread], run_id: str, target, *args) -> None:
        threads[run_id] = threading.Thread(target=target, args=args, name=f"run-{run_id}", daemon=True)
        threads[run_id].start()

    def _advance(self, op) -> None:
        _log.info("picked up %s", op.run_id)
        try:
            state = self.runner.submit(op)
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("advancing %s failed", op.run_id)
            return
        _log.info("%s is %s", op.run_id, state.name)

    def _cancel(self, run_id: str, actor: str) -> None:
        try:
            self.runner.cancel(run_id, actor=actor)
        except InvalidTransition:
            pass  # it ended on its own before the request was delivered
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("cancelling %s failed", run_id)
            return
        _log.info("cancel of %s by %s delivered", run_id, actor)

    def _deliver_turns(self, run_id: str) -> None:
        """Hand the run's waiting turns to its live worker, one at a time and in order.

        A turn asked before the worker is up waits for it: the next pass delivers it.
        """
        track = self.runner.track
        while True:
            waiting = [view for view in self.views.turns(run_id).values() if view.state == "pending"]
            if not waiting:
                return
            worker = self.runner.live_worker(run_id)
            if worker is None:
                return
            view = waiting[0]
            if not hasattr(worker, "turn"):
                intents.answer_turn(track, run_id, view.turn, error="this Cog's worker takes no turns")
                continue
            try:
                answer = worker.turn(view.turn, view.text)
            except Exception as exc:  # noqa: BLE001 - recorded on the Track as the turn's failure
                if getattr(exc, "status", None) == 404 and self._still_opening(view.turn):
                    # The worker was invoked and has not opened its session yet: the next pass tries again.
                    return
                intents.answer_turn(track, run_id, view.turn, error=f"{type(exc).__name__}: {exc}"[:1024])
                continue
            self._first_refused.pop(view.turn, None)
            intents.answer_turn(track, run_id, view.turn, text=answer)
            _log.info("turn %s of %s answered", view.turn, run_id)

    def _still_opening(self, turn: str) -> bool:
        """Whether a worker that holds no session yet is still within its grace to open one."""
        first = self._first_refused.setdefault(turn, time.monotonic())
        if time.monotonic() - first < self.session_grace:
            return True
        self._first_refused.pop(turn, None)
        return False

    def run(self, stop: threading.Event) -> None:
        """Watch the Track until ``stop`` is set."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a Track that cannot be read now is read again next pass
                _log.exception("reading the Track failed")
            stop.wait(self.poll_interval)

    def idle(self) -> bool:
        """Whether nothing is being advanced or cancelled right now."""
        threads = (*self._advancing.values(), *self._cancelling.values(), *self._turning.values())
        return not any(thread.is_alive() for thread in threads)


def _deliveries(specs: list[str]):
    """``COG:NAME`` pairs as what the local executor delivers: each Cog's variables, read when its worker starts."""
    wanted: dict[str, list[str]] = {}
    for spec in specs:
        cog, _, name = spec.partition(":")
        if not cog or not name:
            raise SystemExit(f"--deliver takes COG:NAME, not {spec!r}")
        wanted.setdefault(cog, []).append(name)

    def deliver(cog: str, run_id: str, instance: str) -> dict[str, str]:
        return {name: os.environ[name] for name in wanted.get(cog, ()) if name in os.environ}

    return deliver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The run controller: advances the runs submitted to a Track.")
    parser.add_argument("--track", required=True, help="the SQLite Track file the API writes submissions to")
    parser.add_argument("--packages", action="append", required=True, metavar="DIR",
                        help="a directory Cog packages are found under; repeat for several")
    parser.add_argument("--allow", action="append", metavar="NAME",
                        help="a package that may run; every package under --packages when omitted")
    parser.add_argument("--work-dir", required=True, help="where each run's worker output goes")
    parser.add_argument("--backend", default="none")
    parser.add_argument("--location", default="local", choices=AGENT_LOCATIONS)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--deliver", action="append", default=[], metavar="COG:NAME",
                        help="deliver the variable NAME from this process's environment to the workers of COG, "
                             "and to no other; repeat for several. How a Cog gets its model until bindings do it")
    parser.add_argument("--interaction-timeout", type=float, default=60.0, metavar="SECONDS",
                        help="how long one interaction with a worker may take; 0 for no limit, which a Cog "
                             "holding a session for as long as someone talks to it needs")
    parser.add_argument("--environment", default="pixi", choices=("pixi", "host"),
                        help="how a package's serve task is run: in its own pixi environment, or directly")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s controller %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per /healthz poll is noise here

    deliveries = _deliveries(args.deliver)

    track_path = Path(args.track)
    SqliteTrackStore.ensure_schema(track_path)
    lock = open(track_path.with_suffix(".host.lock"), "w")  # noqa: SIM115 - held for as long as this controller runs
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another controller, or `make op`, is running on {track_path}: one host at a time on a Track",
              file=sys.stderr)
        return 1

    runner = LifecycleRunner(
        track=SqliteTrackStore(track_path), backend=args.backend, location=args.location,
        location_settings={"packages": args.packages, "allow": args.allow, "work_dir": args.work_dir,
                           "environment": args.environment,
                           "interaction_timeout": args.interaction_timeout or None,
                           "deliver": deliveries})
    controller = RunController(runner, poll_interval=args.poll_interval)
    controller.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    _log.info("watching %s on the %r backend, workers at %r", track_path, runner.backend.name, args.location)
    controller.run(stop)
    # Stopping: runs still in flight are left as they are, and the next controller records them interrupted.
    return 0


if __name__ == "__main__":
    sys.exit(main())
