# Experimental Cog and Op execution

This package is a reference implementation for local integration and discovery.
Python interfaces, the `/invoke` protocol, and Track event schemas may change
without backward compatibility guarantees.

## Execution and recovery

`submit()`, `signal()`, and `retry()` run synchronously until completion, pause,
or failure. The Track stores recovery state. After a process restart, a caller
must resubmit the same incomplete Op; there is no background recovery loop.
A paused run waits for `signal()`, unless a signal was already recorded before
the crash and still needs processing.

Only one caller may advance a run at a time. The submission index does not
serialize advancement. Idempotency keys survive engine recovery, but the
reference worker does not persist results across pod replacement, so completed
side effects may repeat.

`retry()` starts a new attempt for a failed run. Completed runs and runs that
exhausted their duration, token, or cost budget cannot be retried; start a new
run with a new id. Budgets are not reset by retrying. Duration is checked at
step boundaries; it does not interrupt an interaction already in progress.
Token and cost accounting happens after an interaction and can overshoot.

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
kind of failure. The step fails, and the Track's `failed` event keeps the code
as its `error` and the detail as its `reason`. These rules hold on construction
as well as on parsing, so an envelope a worker builds in process cannot
sidestep them.
`ok: true` with a non-empty `problems` list is **not** a failure: the step
completes, and the problems are recorded on `step_completed` for a Gate to
decide — step-declared Gates are #99. A `binding`, when the worker reports one,
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

Paused interactions report usage through `PauseRequest(..., usage=...)`, or a
top-level `usage` field beside HTTP `pause`. The Track records each report before
teardown — pauses and `ok: false` answers included — so spending survives
recovery. Unknown usage from a failed interaction prevents retry from advancing
under a spending limit. These reports are worker-supplied accounting, not
independent metering or hard per-request caps.

## Signals and workers

A resumed worker receives its original `input` and a separate `signal` field.
The field is absent before a signal is supplied; explicit `null` is a signal.
In-memory handlers that accept feedback need a keyword `signal` parameter.
A resume after a pause gets a new attempt key; crash recovery of that attempt
reuses its key.

A signal is external feedback, not a Gate implementation. The current
`PauseRequest` and E2E approval fixture exercise pause/resume transport, and
both are transitional: the [glossary](../docs/GLOSSARY.md) defines a Gate as an
Op-owned decision, and step-declared Gates (#99) retire a Cog's
`{"pause": true}` answer.

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
