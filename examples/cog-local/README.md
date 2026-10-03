# Hermes as a Cog, on your machine

Start a hub, sign in, launch a [Hermes](https://hermes-agent.nousresearch.com) agent as a Cog, see it running, talk to it from [Toad](https://github.com/batrachianai/toad), and stop it. Everything runs on your machine: the hub's API and its run controller are two processes, the sign-in is a real one against a local Keycloak, and Hermes runs as a process of its own, in its own environment.

Each step is one `make` target in this directory. Run `make help` to list them.

## What you need

- [Docker](https://docs.docker.com/get-docker/), for Postgres and Keycloak.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), for the Python environments.

Then, once, from this directory:

```sh
make env      # the CLI, the hub, Toad, pixi, and Hermes's environment
make tools    # checks each one is there
```

`make env` installs [pixi](https://pixi.sh) if it is missing (it gives each Cog its own environment), [Toad](https://github.com/batrachianai/toad) with `uv tool install`, and Hermes Agent into the Hermes Cog's environment, about 400 MB the first time. Open a new shell afterwards if pixi or Toad was new.

## 1. Start the hub

```sh
make hub           # Postgres and Keycloak, in containers
make credentials   # who to sign in as: user dev, password dev
```

Keycloak comes with a realm, `nebari`, and its users. `make credentials` shows the one this example signs in as and checks the realm issues a token for it.

Now the processes, each in a terminal of its own:

```sh
make model         # terminal 1: the model Hermes calls (a fake one, see below)
make api           # terminal 2: the hub's API, on http://127.0.0.1:8000
make controller    # terminal 3: the run controller
```

Or all three in the background with `make start` (logs in `.local/`, stopped with `make shutdown`). The API accepts what you ask for and records it. The controller is the one that starts Cogs. They share a record of every run, the Track, and never call each other.

### Which model

By default Hermes talks to a fake model, an OpenAI-compatible endpoint on port 8090 that answers every prompt with `The fake model heard: ...`. It needs no account and no network, which is what lets CI run this example.

To use a real model, set these in your environment before `make controller` (or `make start`), and skip `make model`:

```sh
export COLLAB_MODEL_BASE_URL=https://openrouter.ai/api/v1   # any OpenAI-compatible endpoint
export COLLAB_MODEL_NAME=anthropic/claude-sonnet-4.5
export COLLAB_MODEL_API_KEY=...                             # read from your environment, never from make's command line
```

The controller hands these to the Hermes Cog's worker, and to no other Cog.

## 2. Sign in with the CLI

```sh
make login         # opens Keycloak in your browser: sign in as dev / dev
make whoami
```

```text
user          3b8ec34f-eaa3-4056-897e-c2179651bc69
name          Dev User <dev@example.com>
organization  dev-org
signed in     yes, token expires 2026-10-03T07:24:43Z
```

No browser on this machine? `make login-token` signs in with a token the realm issues for the same user.

The example keeps its sign-in in `.local/cli`, so it does not touch your own `collab-hub` profile.

## 3. Launch Hermes

```sh
make cogs          # the Cogs this hub can launch: hermes among them
make launch        # collab-hub cog launch hermes --entry session
```

```text
Launched hermes as run-d30f9866404b on the none backend, workers local.
```

The controller starts the Hermes Cog as a process in its own environment, and the Cog starts Hermes and opens a session with it. The run's id is kept in `.local/run` for the next steps; pass `RUN=...` to act on another run.

## 4. See it running

```sh
make list          # collab-hub run list
```

```text
RUN               COG     STATUS   AGE  BY
run-d30f9866404b  hermes  RUNNING  0s   Dev User
```

## 5. Talk to Hermes from Toad

```sh
make connect       # toad acp "collab-hub run connect RUN"
```

Toad opens with the running Hermes as its agent. Ask it a few things: `hello`, `what can you do?`, anything you would ask an assistant. With the fake model each answer is `The fake model heard: ...`; with a real one, it is Hermes answering. Leave Toad with `ctrl+q`; Hermes keeps running.

The same from the CLI, one turn at a time:

```sh
make say TEXT="what can you do?"    # collab-hub run say RUN what can you do?
```

How a prompt travels: Toad speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) to `collab-hub run connect`, which sends it to the hub as one turn of the run. The controller delivers the turn to the Hermes Cog, which speaks ACP to Hermes, and the answer comes back the same way. Toad never reaches Hermes directly, and every turn and its answer are recorded with the run.

## 6. Stop Hermes

```sh
make stop          # collab-hub run terminate RUN
make list
```

```text
run-d30f9866404b ended CANCELLED.
RUN               COG     STATUS     AGE  BY
run-d30f9866404b  hermes  CANCELLED  4s   Dev User
```

The controller stops the Cog's process and Hermes with it, and the run takes no more turns. Saying `bye` to it ends the session too, and the run then ends `COMPLETED` instead.

## 7. Shut down

```sh
make shutdown      # what `make start` started
make down          # the containers; their data is kept
make clean         # this example's sign-in, run id and logs
```

## All of it, in one command

```sh
make demo
```

Every step above in order, with the fake model, `login-token` for the sign-in, and a scripted ACP client, `acp_check.py`, where Toad would be. It checks each answer and fails on the first one that is wrong. CI runs it.

## How it fits together

```text
make login     ──▶ Keycloak (dev / dev) ──token──▶ collab-hub
make launch    ──▶ POST /v1/runs ──▶ API ──writes──▶ Track ◀──watches── controller ──starts──▶ Hermes Cog ──▶ hermes acp
make connect   ──▶ Toad ──ACP──▶ collab-hub run connect ──POST /v1/runs/{id}/turns──▶ API ──▶ Track
                       controller ──POST /turn──▶ Hermes Cog ──ACP──▶ Hermes ──▶ model
make stop      ──▶ POST /v1/runs/{id}/cancel ──▶ API ──▶ Track ──▶ controller stops the Cog and Hermes
```

The Hermes Cog is [`cogs/hermes`](../../cogs/hermes/README.md) at the root of the repository: a worker that serves the hub's seam and drives Hermes over ACP, and a pixi package that pins Hermes Agent. Its README says what it does with tools, its model and its home directory.

This directory also has [`cogs/hello`](cogs/hello), the smallest Cog that holds a session, with no model at all. Launch it with `make launch COG=hello`; it answers `help`, `sum 1 2 3`, `whoami` and a few more. It is the one to copy to write a Cog of your own: change `Session.answer` in `serve.py`, run `pixi lock`, and launch it by its directory's name. The controller finds Cogs in `cogs/` at the root, here, and in `dev/cogs`.

## When something is off

| What you see | Why | What to do |
|---|---|---|
| `make api` stops with Postgres or Keycloak errors | The containers are not up | `make hub` |
| `make login` says the hub runs dev auth | Another API, `make -C ../../dev api`, holds the port | Stop it, or `make ... PORT=8010` on every step |
| The run stays `SUBMITTED` | No controller is running | `make controller`, or `make start` |
| Hermes answers `API call failed ... Connection error` | Nothing listens at the model's address | `make model`, or check `COLLAB_MODEL_BASE_URL` |
| The run fails with `model-unavailable` | The controller had no model to hand Hermes | Start the controller from this directory, or set the three variables above |
| The first launch takes minutes | pixi is installing Hermes's environment | `make env` installs it ahead of time |
| `make connect` or `make say` says the run takes no turns | The run has ended | `make launch` again |
| `toad: command not found` | Toad was installed into `~/.local/bin` | Add it to `PATH`, or open a new shell |

The Cog's own output, Hermes's included, is under `../../dev/.local/runs/`, and the processes' in `.local/` when `make start` started them.

## What this does not show yet

- **Hermes's tools.** Hermes runs with none here: it answers, and does nothing else, not even read a file. Giving it tools through the hub, and asking you in Toad before it uses one, is later work.
- **A restart.** The controller runs on the `none` backend, which keeps nothing: stop it while Hermes runs and the run is recorded `INTERRUPTED` when a controller starts again.
- **A deployed hub.** The run API is behind the `cog_runs` feature flag, off by default, and Hermes here is a process on the controller's machine. On a cluster a Cog runs as a pod, a later phase of [`COG_EXECUTION.md`](../../COG_EXECUTION.md).

More: [`docs/cog-execution/runs.md`](../../docs/cog-execution/runs.md) for the run API and the controller, [`cli/README.md`](../../cli/README.md) for every CLI command.
