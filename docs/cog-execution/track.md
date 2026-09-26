# The Track: event schema v1

The Track is the append-only record of one run: which Cogs ran under which
bindings, what came out, which Gates escalated, who decided, and how it ended.
It is the only source of a run's status (ADR-0002 D3), and it is the
accountability record — a Gate signature is worth something because the Track
can say what was signed. This page is the reference for what the Track holds.
The code is `collab_hub_execution.track` and the engine that writes it,
`collab_hub_execution.orchestration`.

## Events

An event is `{run_id, event_type, payload, occurred_at, event_id, sequence, schema}`.
The store assigns `sequence`, a global order that replay follows; `event_id`
is unique, so an event appended twice is refused; `schema` is the version of
the event's shape, `1` for everything this page describes. Every event of a
run carries `run_id`; those about one step carry `step`, and those about one
attempt of it carry `attempt`.

Three kinds of event share the Track:

- **run events** move the run's state machine (`docs/cog-execution/states.md`),
  and a run's status is its Track folded through that machine;
- **step facts** record what one step attempt did and produced;
- **worker facts** record the lifecycle of the worker that served it.

### Run events

| Event | Payload | Written when |
|---|---|---|
| `op_submitted` | `op` — the Op as submitted, its steps with their Gates | a run is submitted; one per run, which the store enforces |
| `run_picked_up` | — | a controller takes the run |
| `gate_escalated` | `step`, `attempt`, `reason`, `escalation` (its id), `envelope` (the result the Gate escalated), `usage`, `approvers` (roles that may decide), `gate` (the policy) | a step's Gate escalates its result |
| `gate_decided` | `step`, `outcome` (`approve`, `send_back`, `reject`), `escalation` (the id answered), `actor`, `value` (the findings), `envelope_digest` (a stable id of the result decided on) | a person decides an escalation |
| `completed` | — | every step completed |
| `failed` | `step`, `error`, `reason`, and what the failure carried (`problems`, `binding`, `worker_error`, `revise_limit`) | the run fails; `step_failed` precedes it for a step's failure |
| `budget_exceeded` | `step`, `dimension` (`duration`, `tokens`, `cost`), `reason` | a budget stops the run |
| `cancelled` | `actor` | a client cancels |
| `interrupted` | `backend` | the host stopped under `none` and the run cannot resume |
| `retry_requested` | `from_status`, `attempt` (`same` or `new`), `budget_epoch` | a run is retried |

### Step facts

| Event | Payload |
|---|---|
| `step_started` | `step`, `cog`, `digest`, `attempt` |
| `interaction_usage` | `step`, `attempt`, `usage` — what one interaction spent, `null` when unknown |
| `step_completed` | `step`, `attempt`, `cog`, `digest`, `binding`, `problems`, `usage`, `frames`, `escalation` (when an approval completed it), and the result: `payload` inline, or `payload_ref` naming where it is kept |
| `step_failed` | `step`, `attempt`, `key` (the idempotency key), `cog`, `digest`, `error` (the envelope's code, or the exception's class), `message` (bounded to 1024 characters), and `problems` and `binding` when the failure carried them |

`step_completed` alone answers "what produced this": the Cog and its digest,
the binding, the problems the Cog reported, the usage it spent, and the Frames
it was given (`frames` is empty until Frames are delivered to steps). `label`
is reserved on step events for sensitivity labels (#13) and is not written yet.

**Payloads by reference.** A result whose JSON is larger than the engine's
`payload_inline_max_bytes` (64 KiB by default) is kept beside the Track under
a reference `run_id/step/attempt/<id>`, and the event carries `payload_ref`
instead of `payload`. `TrackStore.get_payload(ref)` returns it. Everything
else about the step stays on the event, so a reader that never fetches
payloads still knows what produced them.

### Worker facts

`materialized` (`cog`, `digest`), `ready` (`cog`), `interaction_started`
(`step`, `entry_point`), `idle` (`step`), `teardown_started` (`step`,
`reason`) and `teardown_failed` (`step`, `error`) are the worker machine's
records. They leave the run's state where it is.

## Reading a Track written before v1

Events written before schema v1 carry `schema` 0 and are never rewritten.
`collab_hub_execution.track.upgrade` reads one in the v1 shape, keeping its
identity, sequence and time:

| Written as | Read as |
|---|---|
| `submitted` | `op_submitted` |
| `paused` | `gate_escalated` |
| `signal_received` | `gate_decided` with `outcome: send_back` |
| `rejected` | `gate_decided` with `outcome: reject` |
| `timed_out` | `budget_exceeded` with `dimension: duration` |
| `step_completed` with `output` | `step_completed` with `payload` |

`derive_run_status` and the engine read every Track through it, so a run left
waiting by an older engine can still be decided. A pre-v1 escalation has no
`escalation` id, `envelope` or `approvers`: it reads with those empty, a
decision names its id as `None`, and an approval is refused since no result was
recorded to accept — a send back asks for the work again.

## Stores

`TrackStore` is one protocol with three implementations, and one conformance
suite (`execution/tests/test_track_conformance.py`) runs against all of them:

- **`InMemoryTrackStore`** — tests and one-process use.
- **`SqliteTrackStore`** — one file, WAL mode, shared by the processes of one
  host: dev level 1 will run the API and the run controller over
  `dev/.local/track.sqlite`, and the desktop's local run host keeps its Track
  the same way. `SqliteTrackStore.ensure_schema(path)` creates the file and
  its tables.
- **`PostgresTrackStore`** — the hub. Its tables, `collab_track_events` and
  `collab_track_payloads`, come from the `collab_` migration registry
  (version 12) that the API runs at startup, never from the store's own
  `ensure_schema`, which exists for the standalone package and local use. The
  two carry the same DDL, and a change to one is a change to both.

Every store assigns sequences, refuses a duplicate `event_id`, and refuses a
second `op_submitted` for a run (`OneSubmissionPerRun`), so two callers can
never both start it.

**A stream never skips an event.** A reader follows a run with a cursor: the
last sequence it saw. On Postgres a sequence is drawn when a row is inserted
but the row appears when it commits, and two transactions can commit in the
other order — a reader that saw the later one would move past the earlier one
for good. So `PostgresTrackStore` serializes appends to one run: each takes a
transaction-scoped advisory lock on the run before its insert draws a
sequence, and holds it until it commits, so within a run sequence order is
commit order. Appends to different runs do not wait for each other. SQLite and
the in-memory store already write one event at a time.
