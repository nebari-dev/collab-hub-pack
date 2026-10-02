"""The launcher of a local worker: it makes sure the worker does not outlive its controller.

    python -m collab_hub_execution.locations.launcher --stdout OUT --stderr ERR --cwd DIR -- COMMAND...

A child process survives its parent being killed, on Linux and macOS alike, so
"it is a child of the controller" is no guarantee. The executor starts each
worker through this launcher instead. The launcher's stdin is a pipe whose
other end only the controller holds. The launcher starts the worker in a
process group of its own, says which on stdout, and then waits: when the pipe
closes — the controller closed it to tear the worker down, or the controller
died, by ``SIGKILL`` included, and the kernel closed it — the launcher kills the
worker's whole process group and exits. The Cog knows nothing of this.

On Linux the worker's first process also asks the kernel to kill it when the
launcher itself dies (``PR_SET_PDEATHSIG``), a second line for a launcher that
was killed before it could act.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time

_PR_SET_PDEATHSIG = 1


def _die_with_launcher(launcher: int) -> None:
    """In the worker, before exec: on Linux, be killed when the launcher dies.

    The request is not retroactive: a launcher that died between the fork and
    the request left no signal to deliver. So the worker checks afterwards that
    its parent is still the launcher, and ends itself if it is not.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - a second line; the process group kill is the first
        return
    if os.getppid() != launcher:
        os.kill(os.getpid(), signal.SIGKILL)


def _kill_group(pgid: int, child: subprocess.Popen, grace: float) -> None:
    """End every process of the worker's group: asked first, then killed."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            child.poll()
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
    child.poll()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--grace", type=float, default=3.0, help="seconds between SIGTERM and SIGKILL")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command to launch")

    stopping = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.append(True))

    launcher = os.getpid()  # read before the fork, for the worker to compare its parent with
    with open(args.stdout, "ab") as out, open(args.stderr, "ab") as err:
        try:
            child = subprocess.Popen(  # noqa: S603 - the Cog package's own declared serve command
                command, cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True, preexec_fn=lambda: _die_with_launcher(launcher),
            )
        except OSError as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
            return 127
    pgid = child.pid  # start_new_session makes the child the leader of a new group
    print(json.dumps({"pid": child.pid, "pgid": pgid}), flush=True)

    lifeline = sys.stdin.fileno()
    while not stopping:
        if child.poll() is not None:
            break
        try:
            readable, _, _ = select.select([lifeline], [], [], 0.2)
        except InterruptedError:
            continue
        if readable and not os.read(lifeline, 4096):
            break  # the controller closed the pipe, or died
    _kill_group(pgid, child, args.grace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
