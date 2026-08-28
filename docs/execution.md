# Cog and Op execution

A **Cog** is a self-running worker with declared entry points. An **Op** is a
run composed from one or more Cogs. A **Track** is the append-only history of
an Op: its status is derived from Track events rather than kept as a separate
piece of mutable state.

The reference implementation lives in
`api/src/collab_hub_api/execution/`. It defines four boundaries:

- `Executor` materializes and tears down a Cog. It is the only boundary that
  knows where a worker runs.
- `Orchestrator` starts an Op, resolves its Cogs, interacts through declared
  entry points, and observes state.
- `TrackStore` appends, replays, and streams Track events.
- `Resolver` matches an Op's required capabilities with a Cog's provided
  capabilities.

`InMemoryOrchestrator` is the walking skeleton for follow-up work. It uses the
same lifecycle for model, harness, context, and complete Cog classes; those
classes are declared data, not separate inheritance hierarchies.

The in-memory adapters are not a production execution service. Kubernetes,
durable storage, and a production orchestration engine should be added as
adapters behind these boundaries when their respective issues are implemented.

## Reusable test pattern

Create a `CogDefinition`, register a fake handler with `InMemoryExecutor`,
construct an `InMemoryOrchestrator`, submit an `Op`, and assert the Track event
sequence and derived status. Follow-up tests should assert lifecycle
transitions and boundary behavior through the protocols rather than reaching
into a concrete executor or orchestration engine.
