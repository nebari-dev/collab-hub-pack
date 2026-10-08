# Running Ops: backends, statuses, and what a restart does

How a run advances, what its status means, and what survives when the process
advancing it stops. The code is `collab_hub_execution.runner` (the lifecycle
runner), `collab_hub_execution.backends` (the durability backends) and
`collab_hub_execution.locations` (the agent locations); the
decisions behind them are [ADR-0002](../adr/0002-lifecycle-runner-durability-and-placement.md)
D1–D3 and D12. The states and events each status comes from are
[states.md](states.md); what the Track records is [track.md](track.md).

## Durability backends

A run is advanced by the lifecycle runner: its step functions (`resolve`,
`materialize`, `interact`, `read_envelope`, `teardown`, `evaluate_gate`, then
`complete`, `escalate` or `fail`) and the one driver that sequences them. A
*durability backend* is handed each step function by that driver and decides
only how the call is scheduled and whether what it returned is checkpointed. It
holds no lifecycle logic, and it never says what a run's status is: the Track
does.

| Backend | Setting | Built | A run in flight when its host stops |
|---|---|---|---|
| `none` | `backend="none"` (the default) | yes | is recorded `interrupted` when a host starts, and never resumes; a person retries it |
| `dbos` | `backend="dbos"` | Phase 26 of the plan (#104) | resumes from its last completed step, on Postgres or SQLite |
| `temporal` | `backend="temporal"` | Phase 32 (#110) | resumes, its steps run as Temporal activities |

The setting is the only switch: nothing imports a backend, and a runner
configured with a backend that is not built yet refuses to start
(`BackendNotImplemented`), so the configuration shape is fixed before the
backends exist.

## Agent locations

Where a step's worker runs is the *agent location*, a second setting beside the
backend. The runner drives every location the same way: it asks an executor to
materialize a worker, speaks the seam to it (`GET /healthz`, `POST /invoke`),
and asks the executor to tear it down. An executor holds no lifecycle logic.

| Location | Setting | Built | A worker is | For |
|---|---|---|---|---|
| `local` | `location="local"` | yes | a process on the controller's host: the Cog package's `serve` task, in the package's own pixi environment, on a loopback port | development and the desktop |
| `remote` | `location="remote"` | Phase 21 of the plan (#6) | a workload on a cluster, reached over the cluster's network | a Kubernetes hub, always |

The setting is the only switch: nothing imports an executor, and a runner
configured with a location that is not built yet refuses to start
(`LocationNotImplemented`). The Kubernetes executor that exists today is what
`remote` will select; until Phase 21 it is handed to a runner directly, as the
in-memory executor of the tests is.

**What a local worker is given.** Its environment, and nothing else of its
controller: `PATH`, `HOME` and the few variables a process needs to run at all;
`COLLAB_COG_HOST` (always `127.0.0.1`) and `COLLAB_COG_PORT`, where it must
listen; `COLLAB_COG_ID` and `COLLAB_RUN_ID`; `COLLAB_RUN_TOKEN`; and whatever
the binding delivers. Delivery is asked once per worker, for that Cog, run and
step, and enters that one child's environment only: never another worker's,
never the Track, never a file. The controller's own configuration and credentials are
not passed on. Its stdout and stderr go to a directory of its run,
under `<work_dir>`, one level per run and one per step attempt, each named by a
readable prefix of the id and a digest of all of it, so two ids never share a
directory. `worker_started` gives the path as `logs`. The controller reaches a
worker directly on loopback, ignoring any proxy its own environment names.

**What a local worker leaves.** Nothing. Teardown kills the worker's whole
process group. A controller that dies without tearing down, killed with
`SIGKILL` included, still leaves no worker: each worker is started through a
launcher that holds a pipe from the controller, and kills the worker's process
group when that pipe closes, for whatever reason. On Linux the worker is also
killed by the kernel if the launcher itself dies.

**Packages.** At `local` a Cog's name resolves through the *directory package
source*: an allowlisted name under a configured root, refused when the name or
a symbolic link leaves the root. The package is a directory with a `pixi.toml`
that declares a `serve` task and the `pixi.lock` that pins its environment,
both files of the package itself: a package without its lock, or with a
symbolic link in place of either, is refused, and pixi runs it `--locked`. It
is identified on the Track by its name and the sha256 of its manifest and lock, so a development run is never mistaken for a
published Cog. Resolving a published reference is Phase 22.

**The run token.** One per worker, minted when it is materialized. The
controller presents it as a bearer token on `/invoke`; the worker will present
it to the hub endpoints it calls. `worker_started` records its sha256 on the
run's Track, the store the API and the controller share, and
`collab_hub_execution.run_tokens.verify` answers whether a token belongs to a
worker of that run that is still up. It expires when the worker's
`worker_stopped` is recorded, or when the run ends or is interrupted.

`local` is not for a deployed hub: a local worker shares its controller's host
and network identity, so the isolation a cluster gives a worker does not hold.

## The run controller and the run API

The process that accepts runs is not the one that advances them (ADR-0002 D4).

- **The API** (`/v1/runs`, behind the `cog_runs` [feature flag](../feature-flags.md)) records intent on the
  Track and reads a run's status from it. It constructs no executor and never
  calls the controller. The code is `collab_hub_execution.intents`.
- **The run controller** (`python -m collab_hub_execution.controller`) watches the
  Track. It starts each run that was submitted and not yet picked up on the
  lifecycle runner, and delivers each request to cancel, which tears the run's
  worker down and ends it `CANCELLED`. It alone constructs an executor.

| Route | What it does |
|---|---|
| `POST /v1/runs` | Submit an Op: `steps`, each with `name`, `cog`, `entry_point`, `input` and `gate`, and optionally the run's own `name`, a label for listings. Records `op_submitted` with who submitted it, and answers 201 with the run, `SUBMITTED`. A `cog` that is not a package the controller can launch is a 422 naming the packages it can |
| `GET /v1/runs` | The caller's organization's runs, newest first; `status`, `limit` and `offset` |
| `GET /v1/runs/{id}` | One run: its status, each step's state, and each completed step's `output` |
| `GET /v1/runs/launchable` | The Cog packages a step's `cog` may name |
| `POST /v1/runs/{id}/turns` | Ask a Cog whose step holds a session something: records `turn_requested` with the caller and answers 202 with the turn, `pending`. A run that has ended is a 409 |
| `GET /v1/runs/{id}/turns/{turn}` | The turn: `pending`, then `answered` with the Cog's text, or `failed` with why |
| `POST /v1/runs/{id}/cancel` | Records `cancel_requested` with the caller, once, and answers 202. A run that has ended is a 409 naming its status, checked as the request is written |

Every answer names the `backend` and the `location` the run is advanced with,
so a client never assumes a run survives a restart or that its worker is
isolated. A step that was running, or waiting at its Gate, when its run ended
is reported in the state the run ended in. A run belongs to the organization that submitted it; to any other it
is a 404. The routes are authenticated, like every route the protection map
does not open.

This is the controller's and the API's first form, enough for one host:

- The Track is a SQLite file both processes open (`runs.track_path` for the
  API, `--track` for the controller). Postgres, and pickup that two controller
  replicas can race for, come with the run controller's own phase of the plan.
- One controller per Track: it holds a lock beside the file, and a second one
  refuses to start. When it starts, every run a stopped controller left
  unfinished is recorded `interrupted`.
- The controller polls the Track. Event streams, payloads by reference, Gate
  decisions and retry are later phases; until then a run waiting at a Gate can
  only be cancelled.

**Turns.** Some entry points hold a session: `hello`'s `session` keeps its
`/invoke` open and answers turns until it is told `bye` or its run is
terminated. A client asks for a turn through the API; the controller delivers
waiting turns to the run's live worker, one at a time and in order, on the
worker's `POST /turn`, and records the answer (`turn_answered`) or why there
is none (`turn_failed`). A turn asked before the worker is up waits for it; one
still waiting when the run ends fails with it. Nothing reaches a worker but
through the controller, and every turn and its answer are on the Track. A
worker that holds no session answers `/turn` with 404: within 30 seconds of the
first such answer the controller takes it for a session still opening and tries
again, after that it fails the turn and leaves the run as it was. A run waiting
at a Gate takes no turns (409), since nothing can answer them until the Gate is
decided.

Both processes read a run incrementally (`intents.RunViews`): each read asks
the Track only for the events after the last one seen, and rebuilds a run's
view only when it has new ones, so listing runs and watching them cost what
changed rather than all of their history. A run whose Track cannot be replayed
is logged once and left out of listings and of the controller's passes; it
never hides another.

The `collab-hub` CLI is a client of these routes (`cog launch`, `cog list
--launchable`, `run list`, `run show`, `run watch`, `run say`, `run connect`,
`run terminate`); `run connect` serves a run as an
[ACP](https://agentclientprotocol.com) agent, so an ACP client such as Toad
talks to the Cog turn by turn. `run list` gives, in its `CONNECT` column, the
command such a client starts for each run that has not ended: ACP is spoken on
a command's stdin and stdout, so a command, naming the CLI by its path, its
configuration directory when one was chosen, and the hub, is what a client
connects with, from any shell.
[`examples/cog-local`](../../examples/cog-local/README.md) walks through all of
it on one machine.

## Statuses

A run's status is its Track replayed through the run machine
([states.md](states.md)). The ones a caller acts on:

| Status | Meaning | What can happen next |
|---|---|---|
| `SUBMITTED` | recorded, not yet picked up | it starts; `cancel` |
| `RUNNING` | a host is advancing it | `cancel`; if its host stops, `interrupted` |
| `WAITING_AT_GATE` | a step's Gate escalated its result | `decide` (approve, send back, reject); `cancel` |
| `COMPLETED` | every step completed | — |
| `FAILED` | a step produced no result, or its worker could not be torn down | `retry`, as a new attempt |
| `REJECTED` | a person rejected an escalated result | — |
| `CANCELLED` | a person cancelled it; the Track names who | — |
| `BUDGET_EXCEEDED` | a duration, token or cost budget ran out | a new run |
| `INTERRUPTED` | its host stopped under `none`, which cannot resume it | `retry`, continuing the attempt in flight |

## What a restart does under `none`

`none` checkpoints nothing. So when a host starts, `LifecycleRunner.start()`
records every run the Track shows `RUNNING` or `WAITING_AT_GATE` — one a stopped
host left behind — as `interrupted`, with the backend that could not resume it,
and returns their ids. A run left looking alive would be a lie the status could
never correct.

- **An interrupted run is never resumed.** Submitting it again returns its
  status. Starting another host interrupts nothing more.
- **Retrying continues the attempt that was in flight.** `retry()` records
  `retry_requested` with `attempt: same`, so the step runs again under the same
  idempotency key, and a worker that honours the key — the keyed claim, #102 —
  answers instead of acting twice. Steps already completed do not run again.
- **A pause does not survive either.** A run waiting at a Gate when its host
  stopped is interrupted too (decision 3 of the plan); its retry runs the
  escalated step again.
- **A failed run retries as a new attempt**, under a new key, since its failure
  was recorded.
- **A run submitted and never picked up** had nothing in flight, so it is not
  interrupted; submitting it starts it.

One host per Track until run pickup (#121): `start()` takes every unfinished
run on the Track as its own, so two hosts sharing a Track would interrupt each
other's live runs. At dev level 1, `make op` holds a lock on its Track for that
reason.

## Cancelling a run

`cancel(run_id, actor=...)` ends a run `cancelled` and records the actor.

- **A run the host is advancing** is cancelled at its next step boundary. Its
  live worker is torn down at once, so an interaction in flight stops; a worker
  still being brought up is torn down before it is invoked. The call advancing
  the run then records `cancelled`, and keeps no result from an interaction
  that was in flight. `cancel` returns the status the run has until then.
- **A run submitted, running on no host, or waiting at a Gate** is cancelled at
  once.
- **An ended run** cannot be cancelled, and that includes one that ends while
  the request is on its way: a cancel and a run's end are never both recorded.
- **A worker that could not be torn down** is not hidden by the cancel: the
  teardown is tried again, and if that fails too, a `step_failed` with
  `TeardownFailed` and the executor's error is recorded beside `cancelled`.

## One run, one call at a time

Within a host, every call that moves a run — `submit`, `retry`, `decide`,
`cancel` of a run nothing is advancing, and `start` — claims the run before it
reads or writes it, so no two of them move one run at once. A cancel is taken
only while the driver advances the run, under the same lock the driver holds to
write a step's result or the run's end, so the cancel lands before one of those
writes or not at all. A second `submit` of a run another call is moving waits until
that call has written the submission, is refused if its Op differs, and returns
the run's status otherwise; a `retry` or `decide` of it is refused; `start`
leaves it alone.

## Trying it

At dev level 1, with no container: `make -C dev op OP=<name>` runs an Op from
`dev/ops/` with the fake Cogs of `dev/cogs/` on `none`, over a SQLite Track in
`dev/.local/`, and prints its Track and status. The fake Cogs answer in the
same process; add `LOCATION=local` (which needs pixi) and each step's worker is
a real process, whose `worker_started` and `worker_stopped` appear on the
Track and whose output is under `dev/.local/runs/`. `make -C dev api` and
`make -C dev controller` are the two processes of the section above, and
[`examples/cog-local`](../../examples/cog-local/README.md) walks a Cog through
its whole life on them, signed in through Keycloak: `make demo` there runs it. Stop `make op OP=slow` mid-step,
and the next `make op` reports the run `interrupted`. See
[dev/README.md](../../dev/README.md#running-cogs-and-ops).
