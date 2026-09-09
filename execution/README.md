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

## Signals and workers

A resumed worker receives its original `input` and a separate `signal` field.
The field is absent before a signal is supplied; explicit `null` is a signal.
In-memory handlers that accept feedback need a keyword `signal` parameter.
A resume after a pause gets a new attempt key; crash recovery of that attempt
reuses its key.

A signal is external feedback, not a Gate implementation. The current
`PauseRequest` and E2E approval fixture exercise pause/resume transport.
The [glossary](../docs/GLOSSARY.md) defines a Gate as an Op-owned decision.
This experimental implementation does not yet implement step-owned Gates or
the [result envelope](../docs/cog-execution/result-envelope.md).

`KubernetesCogExecutor(interaction_timeout=300)` sets the default worker HTTP
client's timeout in seconds; `None` disables it. An injected `worker_http`
controls its own timeout. This is an HTTP timeout, not a run deadline.
Only connection failures are retried automatically. Read/write timeouts and
protocol failures propagate because the worker may already have executed the
request; an HTTP failure does not prove that a side effect did not occur.

## Development

Run `uv run --group test pytest` from this directory. Set `TEST_POSTGRES_URL`
to a disposable database to include the Postgres tests; they recreate Track
tables. The kind workflow also exercises the worker transport and resume path.
