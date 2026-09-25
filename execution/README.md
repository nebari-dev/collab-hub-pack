# Experimental Cog and Op execution

This package is a reference implementation for local integration and discovery.
Python interfaces, the `/invoke` protocol, and Track event schemas may change
without backward compatibility guarantees.

## Execution and recovery

`submit()`, `decide()`, and `retry()` run synchronously until the run completes,
fails, or waits at a Gate, and return the run's state. The Track stores recovery
state. After a process restart, a caller must resubmit the same incomplete Op;
there is no background recovery loop. A run `WAITING_AT_GATE` waits for
`decide()`; once a decision is recorded the run is `RUNNING` again, so a crash
before the run advances resumes on the next submit.

Only one caller may advance a run at a time. The submission index does not
serialize advancement. Idempotency keys survive engine recovery, but the
reference worker does not persist results across pod replacement, so completed
side effects may repeat.

`retry()` starts a new attempt for a failed run. Completed runs and runs that
exhausted their duration, token, or cost budget cannot be retried; start a new
run with a new id. (The run machine allows a retry after a budget stop as a new
budget epoch; the engine does not offer it until #4 builds epochs.) Budgets are
not reset by retrying. Duration is checked at step boundaries; it does not interrupt an interaction already in progress.
Token and cost accounting happens after an interaction and can overshoot.

## States

Every state is one of four state machines in `collab_hub_execution.states`,
built on the state pattern: a Cog's install (`CogInstall`), a worker
(`Worker`), a step attempt under the keyed claim (`StepAttempt`) and a run
(`Run`). Each state is a class behind its machine's interface; the context
object delegates every event to its current state, and an event the state does
not accept raises `InvalidTransition`, naming both. Transitions are pure: an
event returns a `Transition(after, records)` and the engine writes the records
to the Track. The states, their transitions and what each records are
[`docs/cog-execution/states.md`](../docs/cog-execution/states.md), which a test
holds to the code.

The engine returns and `observe()` reports a `RunState` — `RunState.RUNNING`,
`RunState.WAITING_AT_GATE` and so on — sent on the wire as its name in lower
case. `observe()` is the run machine folded over the Track (`Run.replay`, or
`derive_run_status` over a Track), and `None` for a run never submitted. A
duration stop is `RunState.BUDGET_EXCEEDED`, recorded as `budget_exceeded`
with `dimension: duration`. The worker's own states are not the
run's: between steps, or while a worker idles, the run is `RUNNING`.

Each call reads the Track once to act on it.

## Gates and decisions

A Cog cannot pause a run. It reports `problems` in its envelope, and the step's
`Gate` decides what they mean. `OpStep.gate` defaults to `Gate()`, which
escalates any problem with severity `error`; `Gate(escalate=...)` takes
`"never"`, `"error"`, `"warn"` (any problem) or `"always"` (a sign-off on every
result), and `approvers` names the roles that may decide — none means
organization owners and platform operators (`DEFAULT_APPROVERS`). A result
passes, passes with its problems recorded, or escalates.

An escalation records the step, the attempt, the envelope, the Gate's reason
and its approvers, under an escalation id minted over the attempt and the
envelope; `open_escalation(run_id)` returns it, and is on the engine contract
beside `decide()` so a caller can find what a decision must name. An escalation
recorded before Gates existed has no id, envelope or approvers: it reads with
those fields empty, a decision names its id as `None`, and an approval runs the
step again, since no envelope was recorded to complete it with. `decide(run_id, escalation=,
actor=, outcome=, findings=)` answers it:

- `approve` completes the step with the envelope the approver saw — the step
  does not run again — and the run goes on. An escalation recorded before Gates
  holds no result, so approving one is refused: send it back, which asks for the
  work again, or reject it;
- `reject` ends the run `REJECTED`;
- `send_back` re-runs the step with the findings as its `signal`, under a new
  attempt key, and its next result goes through the Gate again, with a new
  escalation id.

`findings` is a sequence of findings. One string, a mapping or a set is refused
rather than sent back as its characters, its keys, or an arbitrary order;
`None` is no findings at all.

Each decision is recorded with its escalation, the actor, the outcome, the
findings and `envelope_digest`, a stable id for the result it decided on, so a
reader of decisions identifies that result without joining to the escalation.

A budget stop never discards a result that was paid for. When an interaction
crosses a spending limit and its result needs review, the escalation is
recorded, and the stop lands at the next boundary — the end of the run
included — by when no further work has been spent. So an approval, which spends
nothing, keeps the work, and the run then stops on its budget.

A Gate recorded on the Track is read, never refused, so a run stays decidable
even after a rollback: a policy this engine does not know reads as `always`,
the strictest, and approvers that are not role names read as none declared. A
Gate a caller *declares* is still refused.

A decision naming an escalation that is no longer open raises
`StaleEscalation` and changes nothing, so a late approval never approves a
revision its reviewer did not see. With `max_revisions=N`, a step may be sent
back N times; the next send back ends the run `FAILED` with error
`revise_limit_exceeded`. Who may decide is the run API's to check (#103); the
engine records who did.

## The Track

Every write to the Track is an event of schema v1
([`docs/cog-execution/track.md`](../docs/cog-execution/track.md)). Three kinds
share it: run events, which are the run machine's records (`gate_escalated`,
`gate_decided`, `failed`, `budget_exceeded` and the rest); step facts, which say
what one attempt did and produced; and worker facts. `step_completed` alone
names what produced a result — the Cog, its digest, the binding, the problems,
the usage, the Frames — with the result inline or, above
`payload_inline_max_bytes` (64 KiB by default), kept by reference and named by
`payload_ref`; `TrackStore.get_payload` returns it. `step_failed` records a
step's failure with its idempotency key, the Cog, the error code or exception
class and a message bounded to 1024 characters, before the run's own `failed`.

A Track written before v1 is never rewritten: `track.upgrade` reads its events
in the v1 shape, and everything that reads a Track goes through it.

Three stores implement `TrackStore` and pass the same conformance suite:
`InMemoryTrackStore`, `SqliteTrackStore` (one file in WAL mode, for the
processes of one host; `SqliteTrackStore.ensure_schema(path)` creates it) and
`PostgresTrackStore`. On the hub the Postgres tables come from the API's
migration registry, never from the store's `ensure_schema`. Every store assigns
sequences, refuses a duplicate event id, and refuses a second `op_submitted`
for a run with `OneSubmissionPerRun`.

## The result envelope

Every `CogWorker.interact()` returns a `ResultEnvelope`, the version-1 envelope
of [`docs/cog-execution/result-envelope.md`](../docs/cog-execution/result-envelope.md).
Over HTTP a worker answers `/invoke` with that envelope as JSON, for example:

```json
{"envelope": 1, "ok": true, "payload": {"answer": "..."}, "problems": [], "usage": {"tokens": 10, "cost": 0.002}}
```

`ResultEnvelope.parse` reads it: the version is `envelope`, unknown fields are
ignored, and anything that is not an envelope — no version, `ok` not a
boolean, `ok: false` without an `error`, `ok: true` with one — fails the step
as `EnvelopeInvalid`. In-memory handlers return a `ResultEnvelope` or an
envelope-shaped mapping; any other value becomes the payload of a successful
envelope with unknown usage.

`ok: false` needs `error: {code, detail}`, with the code one of the five the
envelope document lists — any other code is an invalid envelope, not a new
kind of failure. The step fails: `step_failed` records the code, the detail,
the key and the worker, and the run's `failed` event keeps the code as its
`error` and the detail as its `reason`. These rules hold on construction
as well as on parsing, so an envelope a worker builds in process cannot
sidestep them.
`ok: true` with a non-empty `problems` list is **not** a failure: the step
completes, and the problems are recorded on `step_completed` for a Gate to
decide — see *Gates and decisions*. A `binding`, when the worker reports one,
is recorded there too.

Over HTTP the statuses follow the envelope document: 200 carries an envelope
(`ok` true or false); 422 (`invalid-input`), 502 (`model-call-failed`,
`model-response-malformed`) and 503 (`model-unavailable`, `binding-invalid`)
carry an error envelope, and when the body is not one — a proxy's page, a worker
that died mid-answer — the status alone names the failure with that code. Any
other status is a transport error and fails the step by its exception's name.

## Usage accounting

`usage` is read from the envelope, never from inside `payload`. `tokens` is a
non-negative integer; `cost` is a finite, non-negative number in the same units
as `RunBudget.max_cost`. Reports cover this interaction only, not cumulative
usage.

Missing usage is unknown. A configured token limit requires `tokens`, and a
configured cost limit requires `cost`; explicit zero is valid. Missing required
or malformed usage fails the run with `UsageUnavailable` before another step
starts. With neither spending limit configured, absent usage is allowed.

The Track records each report before teardown — results that escalate and
`ok: false` answers included — so spending survives recovery. Unknown usage from a failed interaction prevents retry from advancing
under a spending limit. These reports are worker-supplied accounting, not
independent metering or hard per-request caps.

## Workers

A step that is sent back receives its original `input` and, separately, the
findings as `signal`. The field is absent until a send back; an empty list of
findings is still a signal. In-memory handlers that are sent back need a
keyword `signal` parameter. Crash recovery of an attempt reuses its key. A
worker that answers `{"pause": true}` is not answering with an envelope, and
the step fails as `EnvelopeInvalid`.

`KubernetesCogExecutor(interaction_timeout=300)` sets the default worker HTTP
client's read/write timeout in seconds (default: 60); `None` disables only those
timeouts. Connect and pool timeouts remain 5 seconds. An injected `worker_http`
controls its own timeout. This is an HTTP timeout, not a run deadline.
Only connection failures are retried automatically. Read/write timeouts and
protocol failures propagate because the worker may already have executed the
request; an HTTP failure does not prove that a side effect did not occur.

## Development

Run `uv run --group test pytest` from this directory. Set `TEST_POSTGRES_URL`
to a disposable database to include the Postgres tests; they recreate Track
tables. The kind workflow also exercises the worker transport and resume path.
