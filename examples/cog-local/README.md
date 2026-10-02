# Launch a Cog from the CLI, on your own machine

The shortest path from nothing to a Cog running: the hub's API and its run controller as two plain processes, a Cog that is one short Python file, and the `collab-hub` CLI to launch it, list it and stop it. No container, no cluster, no sign-in.

```text
collab-hub cog launch hello ──POST /v1/runs──▶ API ──writes──▶ Track ◀──watches── run controller
                                                                                       │ starts
collab-hub run list / show  ──GET /v1/runs───▶ API ──reads───▶ Track ◀──records── hello worker
```

The API only records what you asked for and reads back what happened. The controller, a separate process, is the one that starts the Cog's worker. They share a file, the Track, and never call each other.

## What you need

- [uv](https://docs.astral.sh/uv/), which runs the API, the controller and the CLI.
- [pixi](https://pixi.sh), which gives the Cog its own environment.

## Run it in one go

From the repository root:

```sh
examples/cog-local/demo.sh
```

It starts the API and the controller, launches `hello` twice through the CLI (once to completion, once to terminate it mid-run), checks each result, and stops both processes. The first run takes a little longer while pixi installs the Cog's Python. Set `API_PORT=8010` if port 8000 is taken.

## Run it by hand

Three terminals, all from the repository root.

**1. The hub's API**, at dev level 1: dev auth, no database, and the run API switched on.

```sh
make -C dev api
```

**2. The run controller**, which advances what the API accepts.

```sh
make -C dev controller
```

**3. The CLI.** Point it at the hub once; with dev auth there is nothing to sign in to.

```sh
cd cli
uv run collab-hub --hub http://localhost:8000 login
uv run collab-hub whoami
```

Launch the Cog and follow the run to its end:

```sh
uv run collab-hub cog launch hello --input '{"name": "Ada"}' --watch
```

```text
Launched hello as run-3eb7ab0eaf43 on the none backend, workers local.
run-3eb7ab0eaf43: COMPLETED
run        run-3eb7ab0eaf43
status     COMPLETED
runs on    backend none, workers local
submitted  2026-10-02T16:46:15.475555+00:00 by dev-user

STEP   COG    ENTRY  STATE      ERROR
hello  hello  run    completed

hello answered:
{
  "greeting": "Hello, Ada!",
  "pid": 1104975,
  "run": "run-3eb7ab0eaf43"
}
```

The `pid` is the worker's: a real process the controller started for this run and stopped when the step ended.

Launch one that takes two minutes, see it running, and terminate it:

```sh
uv run collab-hub cog launch hello --input '{"name": "Ada", "seconds": 120}'
uv run collab-hub run list
uv run collab-hub run terminate run-2fd83720b1d8     # the id `cog launch` printed
uv run collab-hub run list
```

```text
RUN               COG    STATUS     AGE  BY
run-2fd83720b1d8  hello  CANCELLED  1s   dev-user
run-3eb7ab0eaf43  hello  COMPLETED  2s   dev-user
```

`run show ID` prints one run again, and every command takes `--json`.

## What is in this directory

| Path | What it is |
|---|---|
| `cogs/hello/serve.py` | The Cog's worker: `GET /healthz` and `POST /invoke`, standard library only |
| `cogs/hello/pixi.toml` | The package: its `serve` task is the command the controller starts a worker with |
| `cogs/hello/pixi.lock` | The environment that task runs in, pinned |
| `demo.sh` | The walk-through above as one script that checks itself; CI runs it |

`make -C dev api` and `make -C dev controller` both look for Cog packages in `dev/cogs` and in `examples/cog-local/cogs`, so `hello` can be launched by name. A name that is in neither is refused, with the names that can be launched.

## Write your own

Copy `cogs/hello` to `cogs/<name>`, change `run()` in `serve.py`, and launch it with `collab-hub cog launch <name>`. Three things make a directory a Cog package here:

- a `pixi.toml` with a `serve` task, and its `pixi.lock` (`pixi lock` writes it);
- a worker that listens on `COLLAB_COG_HOST`:`COLLAB_COG_PORT` and answers `GET /healthz` with 200;
- `POST /invoke`, which checks the bearer token against `COLLAB_RUN_TOKEN` and answers with a result envelope (`docs/cog-execution/result-envelope.md`).

A worker's output is under `dev/.local/runs/`, in the directory its run's `worker_started` event names.

## What this does not show yet

- **A restart.** The controller runs on the `none` backend, which keeps nothing: stop it mid-run and the run is recorded `INTERRUPTED` when a controller next starts.
- **Gates.** A step whose Gate escalates waits at it, and deciding one from the CLI comes later; `--gate never` avoids it.
- **A deployed hub.** The run API is behind the `cog_runs` feature flag, off by default, and a worker here is a process on the controller's host. A cluster runs workers as pods, which is a later phase of [`COG_EXECUTION.md`](../../COG_EXECUTION.md).

More: [`docs/cog-execution/runs.md`](../../docs/cog-execution/runs.md) for the run API and the controller, [`cli/README.md`](../../cli/README.md) for the CLI, [`dev/README.md`](../../dev/README.md) for the dev environment.
