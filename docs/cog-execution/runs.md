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
| `dbos` | `backend="dbos"` | Phase 25 of the plan (#104) | resumes from its last completed step, on Postgres or SQLite |
| `temporal` | `backend="temporal"` | Phase 31 (#110) | resumes, its steps run as Temporal activities |

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
| `remote` | `location="remote"` | Phase 20 of the plan (#6) | a workload on a cluster, reached over the cluster's network | a Kubernetes hub, always |

The setting is the only switch: nothing imports an executor, and a runner
configured with a location that is not built yet refuses to start
(`LocationNotImplemented`). The Kubernetes executor that exists today is what
`remote` will select; until Phase 20 it is handed to a runner directly, as the
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
published Cog. Resolving a published reference is Phase 21.

**The run token.** One per worker, minted when it is materialized. The
controller presents it as a bearer token on `/invoke`; the worker will present
it to the hub endpoints it calls. `worker_started` records its sha256 on the
run's Track, the store the API and the controller share, and
`collab_hub_execution.run_tokens.verify` answers whether a token belongs to a
worker of that run that is still up. It expires when the worker's
`worker_stopped` is recorded, or when the run ends or is interrupted.

`local` is not for a deployed hub: a local worker shares its controller's host
and network identity, so the isolation a cluster gives a worker does not hold.

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
Track and whose output is under `dev/.local/runs/`. Stop `make op OP=slow` mid-step,
and the next `make op` reports the run `interrupted`. See
[dev/README.md](../../dev/README.md#running-cogs-and-ops).
