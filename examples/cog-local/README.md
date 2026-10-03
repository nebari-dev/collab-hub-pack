# A Cog's whole life, on your machine

Start a hub, sign in, launch a Cog, see it running, talk to it from [Toad](https://github.com/batrachianai/toad), and stop it. Everything runs on your machine: the hub's API and its run controller are two processes, the sign-in is a real one against a local Keycloak, and the Cog is a process of its own.

Each step is one `make` target in this directory. Run `make help` to list them.

## What you need

- [Docker](https://docs.docker.com/get-docker/), for Postgres and Keycloak.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), for the Python environments.

Then, once, from this directory:

```sh
make env      # the CLI, the hub's Python, Toad, pixi, and the Cog's own environment
make tools    # checks each one is there
```

`make env` installs [pixi](https://pixi.sh) if it is missing (it gives each Cog its own environment) and [Toad](https://github.com/batrachianai/toad) with `uv tool install`. Open a new shell afterwards if either was new.

## 1. Start the hub

```sh
make hub           # Postgres and Keycloak, in containers
make credentials   # who to sign in as: user dev, password dev
```

Keycloak comes with a realm, `nebari`, and its users. `make credentials` shows the one this example signs in as and checks the realm issues a token for it.

Now the hub itself. It is two processes, so use two terminals:

```sh
make api           # terminal 1: the hub's API, on http://127.0.0.1:8000
make controller    # terminal 2: the run controller
```

Or run both in the background with `make start` (logs in `.local/`, stopped with `make shutdown`). The API accepts what you ask for and records it. The controller is the one that starts Cogs. They share a record of every run, the Track, and never call each other.

## 2. Sign in with the CLI

```sh
make login         # opens Keycloak in your browser: sign in as dev / dev
make whoami
```

```text
user          3b8ec34f-eaa3-4056-897e-c2179651bc69
name          Dev User <dev@example.com>
organization  dev-org
signed in     yes, token expires 2026-10-03T07:11:02Z
```

No browser on this machine? `make login-token` signs in with a token the realm issues for the same user.

The example keeps its sign-in in `.local/cli`, so it does not touch your own `collab-hub` profile.

## 3. Launch the Cog

```sh
make cogs          # the Cogs this hub can launch: hello among them
make launch        # collab-hub cog launch hello --entry session
```

```text
Launched hello as run-c42c387d6e23 on the none backend, workers local.
```

The controller starts `hello` as a process, in its own environment, and opens a session with it. The run's id is kept in `.local/run` for the next steps; pass `RUN=...` to act on another run.

## 4. See it running

```sh
make list          # collab-hub run list
```

```text
RUN               COG    STATUS   AGE  BY
run-c42c387d6e23  hello  RUNNING  0s   Dev User
```

## 5. Talk to it from Toad

```sh
make connect       # toad acp "collab-hub run connect RUN"
```

Toad opens with the Cog as its agent. Try a few commands:

| Say | `hello` answers |
|---|---|
| `help` | what it can do |
| `hello Ada` | `Hello, Ada!` |
| `sum 1 2 3` | `1 + 2 + 3 = 6` |
| `whoami` | the run and the process you are talking to |
| `history` | what you said so far |

Toad speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP). `collab-hub run connect` is the agent it starts: it turns each prompt into one turn of the run, sent to the hub, delivered to the Cog by the controller, and read back. Toad never reaches the Cog directly, and every turn and its answer are recorded with the run. Leave Toad with `ctrl+q`; the Cog keeps running.

The same from the CLI, one turn at a time:

```sh
make say TEXT="sum 1 2 3"    # collab-hub run say RUN sum 1 2 3
```

## 6. Stop the Cog

```sh
make stop          # collab-hub run terminate RUN
make list
```

```text
run-c42c387d6e23 ended CANCELLED.
RUN               COG    STATUS     AGE  BY
run-c42c387d6e23  hello  CANCELLED  2s   Dev User
```

The controller stops the Cog's process, and the run takes no more turns. Saying `bye` to the Cog ends its session too, and the run then ends `COMPLETED` instead.

## 7. Shut down

```sh
make shutdown      # the API and the controller, if `make start` started them
make down          # the containers; their data is kept
make clean         # this example's sign-in, run id and logs
```

## All of it, in one command

```sh
make demo
```

Every step above in order, with `login-token` for the sign-in and a scripted ACP client, `acp_check.py`, where Toad would be. It checks each answer and fails on the first one that is wrong. CI runs it.

## How it fits together

```text
make login     ──▶ Keycloak (dev / dev) ──token──▶ collab-hub
make launch    ──▶ POST /v1/runs ──────▶ API ──writes──▶ Track ◀──watches── controller ──starts──▶ hello
make connect   ──▶ Toad ──ACP──▶ collab-hub run connect ──POST /v1/runs/{id}/turns──▶ API ──▶ Track
                                                         controller ──POST /turn──▶ hello ──answer──▶ Track
make stop      ──▶ POST /v1/runs/{id}/cancel ──▶ API ──▶ Track ──▶ controller stops hello
```

The Cog is the directory [`cogs/hello`](cogs/hello):

| File | What it is |
|---|---|
| `serve.py` | The worker: `GET /healthz`, `POST /invoke` for its two entry points, and `POST /turn` while a session is open. Standard library only |
| `pixi.toml` | The package: its `serve` task is the command the controller starts the worker with |
| `pixi.lock` | The environment that task runs in, pinned |

`hello` has two entry points. `session`, launched above, stays open and answers turns until it is told `bye` or its run is terminated. `run` answers once: `collab-hub cog launch hello --input '{"name": "Ada"}' --watch`.

To write your own, copy `cogs/hello` to `cogs/<name>`, change `Session.answer` in `serve.py`, run `pixi lock` in it, and `collab-hub cog launch <name> --entry session`. The controller finds Cogs here and in `dev/cogs`.

## When something is off

| What you see | Why | What to do |
|---|---|---|
| `make api` stops with Postgres or Keycloak errors | The containers are not up | `make hub` |
| `make login` says the hub runs dev auth | Another API, `make -C ../../dev api`, holds the port | Stop it, or `make ... PORT=8010` on every step |
| The run stays `SUBMITTED` | No controller is running | `make controller`, or `make start` |
| `make connect` or `make say` says the run takes no turns | The run has ended | `make launch` again |
| `toad: command not found` | Toad was installed into `~/.local/bin` | Add it to `PATH`, or open a new shell |
| The first launch takes a while | pixi is installing the Cog's Python | `make env` installs it ahead of time |

The Cog's own output is under `../../dev/.local/runs/`, and the API's and the controller's in `.local/` when `make start` started them.

## What this does not show yet

- **A restart.** The controller runs on the `none` backend, which keeps nothing: stop it while the Cog runs and the run is recorded `INTERRUPTED` when a controller starts again.
- **A deployed hub.** The run API is behind the `cog_runs` feature flag, off by default, and a Cog here is a process on the controller's machine. On a cluster a Cog runs as a pod, a later phase of [`COG_EXECUTION.md`](../../COG_EXECUTION.md).
- **A model.** `hello` answers by itself. The Hermes harness Cog, which answers with a model, is a later phase of the plan, and runs the same way.

More: [`docs/cog-execution/runs.md`](../../docs/cog-execution/runs.md) for the run API and the controller, [`cli/README.md`](../../cli/README.md) for every CLI command.
