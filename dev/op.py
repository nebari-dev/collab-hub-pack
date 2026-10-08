"""`make op OP=<name>`: run a fake Op at dev level 1 and print its Track.

The Op is `dev/ops/<name>.yaml`; each step names a fake Cog under `dev/cogs/`,
a Cog package whose `serve.py` holds its `handle` function. Without a location,
`handle` answers in this process: no worker, no container, no network. With
`LOCATION=local` each step's worker is a real process: the package's `serve`
task, run in its own pixi environment through the local executor, listening on
a loopback port, and gone when the step ends or when this host dies. Its output
is under `dev/.local/runs/<run>/`.

The runner uses the durability backend `BACKEND` names (`none` by default, the
only one built) over the SQLite Track in `dev/.local/track.sqlite`, and starts
the way a host does: a run a previous `make op` left unfinished — stopped
mid-step with Ctrl-C — is recorded `interrupted` before the new run starts.

One host at a time: the runner assumes it is the only one advancing the runs on
its Track, and run pickup (Phase 11 of the plan) is what lets hosts share one.
Until then a second `make op` on the same Track refuses to start while another
is running, rather than record `interrupted` for a run the first is still
advancing, whose later writes would leave that run's Track unreadable.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import yaml

from collab_hub_execution import (
    AGENT_LOCATIONS,
    Gate,
    InMemoryCogExecutor,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
    SqliteTrackStore,
)

DEV = Path(__file__).resolve().parent


def _handler(cog: str):
    path = DEV / "cogs" / cog / "serve.py"
    if not path.exists():
        raise SystemExit(f"no fake Cog {cog!r}: expected {path.relative_to(DEV.parent)}")
    spec = importlib.util.spec_from_file_location(f"dev_cog_{cog.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # In process the envelope is checked the way the seam checks one that arrived as JSON.
    return lambda entry_point, value, **feedback: ResultEnvelope.parse(module.handle(entry_point, value, **feedback))


def _op(name: str, run_id: str, in_process: bool) -> tuple[OpDefinition, RunBudget | None, dict]:
    path = DEV / "ops" / f"{name}.yaml"
    if not path.exists():
        known = sorted(p.stem for p in (DEV / "ops").glob("*.yaml"))
        raise SystemExit(f"no Op {name!r}: expected {path.relative_to(DEV.parent)}; the Ops are {', '.join(known)}")
    spec = yaml.safe_load(path.read_text())
    steps = tuple(
        OpStep(name=step["name"], cog=step["cog"], entry_point=step["entry_point"], input=step.get("input"),
               gate=Gate.from_dict(step.get("gate")))
        for step in spec["steps"]
    )
    budget = spec.get("budget")
    limits = None
    if budget:
        seconds = budget.get("max_seconds")
        limits = RunBudget(max_tokens=budget.get("max_tokens"), max_cost=budget.get("max_cost"),
                           max_duration=timedelta(seconds=seconds) if seconds else None)
    handlers = {step.cog: _handler(step.cog) for step in steps} if in_process else {}
    return OpDefinition(run_id, steps), limits, handlers


def _print_track(track, run_id: str) -> None:
    for event in track.replay(run_id):
        payload = event.payload
        detail = " ".join(
            f"{key}={payload[key]}"
            for key in ("step", "attempt", "error", "reason", "dimension", "actor", "backend", "location", "pid")
            if payload.get(key) not in (None, "")
        )
        print(f"  {event.sequence:>5}  {event.event_type:<20} {detail}".rstrip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("op", help="the Op to run: a file under dev/ops/, without .yaml")
    parser.add_argument("--backend", default=os.environ.get("BACKEND", "none"))
    parser.add_argument("--location", default=os.environ.get("LOCATION", ""), choices=("", *AGENT_LOCATIONS),
                        help="where each step's worker runs; without one, the fake Cogs answer in this process")
    parser.add_argument("--track", default=str(DEV / ".local" / "track.sqlite"))
    args = parser.parse_args()

    track_path = Path(args.track)
    track_path.parent.mkdir(parents=True, exist_ok=True)
    host = open(track_path.with_suffix(".host.lock"), "w")  # held for as long as this host runs
    try:
        fcntl.flock(host, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another `make op` is running on {track_path}: one host at a time on a Track, until run pickup "
              "(Phase 11) lets hosts share one", file=sys.stderr)
        return 1
    SqliteTrackStore.ensure_schema(track_path)
    track = SqliteTrackStore(track_path)

    run_id = f"{args.op}-{uuid.uuid4().hex[:8]}"
    op, budget, handlers = _op(args.op, run_id, in_process=not args.location)
    if args.location:
        where = {"location": args.location, "location_settings": {
            "packages": [DEV / "cogs"], "work_dir": track_path.parent / "runs"}}
    else:
        where = {"executor": InMemoryCogExecutor(handlers)}
    runner = LifecycleRunner(track=track, budget=budget, backend=args.backend, **where)
    for interrupted in runner.start():
        print(f"interrupted {interrupted}: a previous `make op` stopped before it ended", flush=True)

    print(f"run {run_id} on the {runner.backend.name!r} backend, workers "
          f"{'at ' + repr(args.location) if args.location else 'in process'}", flush=True)
    try:
        state = runner.submit(op)
    except KeyboardInterrupt:
        print(f"\nstopped mid-run: the next `make op` records {run_id} interrupted", file=sys.stderr)
        return 130
    _print_track(track, run_id)
    print(f"status: {state.name}")
    escalation = runner.open_escalation(run_id)
    if escalation is not None:
        print(f"waiting at the Gate of step {escalation['step']!r}: {escalation['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
