# Running Ops: backends, statuses, and what a restart does

How a run advances, what its status means, and what survives when the process
advancing it stops. The code is `collab_hub_execution.runner` (the lifecycle
runner) and `collab_hub_execution.backends` (the durability backends); the
decisions behind them are [ADR-0002](../adr/0002-lifecycle-runner-durability-and-placement.md)
D1–D3. The states and events each status comes from are
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
`dev/.local/`, and prints its Track and status. Stop `make op OP=slow` mid-step,
and the next `make op` reports the run `interrupted`. See
[dev/README.md](../../dev/README.md#running-cogs-and-ops).
