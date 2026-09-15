# Cog execution — read this first

This directory is the basis for the hub's Cog and Op execution work: the
vocabulary, the boundary between the hub and a Cog, and the contracts that
cross it. Read it before picking up any issue labeled `cog-execution`, and
check work against it in review. The decisions themselves are recorded in
[ADR-0001](../adr/0001-cog-execution.md); this directory explains the terms
and the seam the ADR assumes.

Contents:

- **This page** — the seam in one paragraph, what stays inside a Cog, a
  review checklist, and how the open issues map to the ADR.
- [The glossary](../GLOSSARY.md) — every term used in the issues, the ADR,
  and these docs, defined in one place with citations.
- [The Op–Cog seam](op-cog-seam.md) — the four things an Op step needs from
  a Cog, and why nothing else crosses.
- [The result envelope](result-envelope.md) — the shape a Cog's entry point
  returns; what Guards check, Gates read, and Tracks record.
- [Sensitivity](sensitivity.md) — how data-sensitivity labels are born,
  propagated, and enforced (the basis of ADR-0001 D10).

## Vocabulary

These terms are used precisely throughout the issues, the ADR, and the
code, and they all live in **[the glossary](../GLOSSARY.md)** — one page,
every term, each entry citing the whitepaper section or ADR decision that
defines it. When a word there has a narrower meaning than its everyday
one, the narrower meaning is the one intended. Read the glossary before
the rest of this directory; the notes below assume its vocabulary
(seam, envelope, worker, binding record, Guard vs contract check).

## The seam in one paragraph

An Op step needs exactly four things from a Cog: an invokable task entry
point, a result envelope, a health probe, and a catalog card. Nothing else
crosses. Harness, model, weights, environment, binding machinery — all stay
inside the Cog and remain swappable without the Op layer noticing. This is
what lets the hub build the Op factory against Cogs it treats as services,
and what lets the same Cog run locally later (ADR-0001 D1). The full
argument is in [the seam note](op-cog-seam.md).

## What stays inside a Cog

The hub must not build or own any of these, and a design that needs the
hub to is drifting:

- the Cog's runtime and pinned environment (the package *is* the runtime;
  the hub runs it, it does not build a generic image and inject the Cog's
  context into it);
- the harness — the render → call → parse → check loop around the model;
- the model client and how the Cog authenticates to its bound model;
- resolution logic — the hub supplies the satisfier *inventory* and records
  the *result*; the Cog's `resolve` entry point selects;
- contract checks on the Cog's own output;
- any notion of approval, pause, or revise loop — those are Op-layer
  policy, declared on the step and evaluated by the engine from the
  envelope.

## Review checklist

Ask these of every `cog-execution` change; each maps to an ADR-0001
invariant or a seam rule.

1. Does the orchestrator touch the Cog only through declared entry points?
   (Invariant 5.) A step that reaches into a package, builds a prompt for
   the Cog, or supplies its model client has flattened the Cog into a DAG
   step.
2. Are cluster and engine primitives confined to the executor and engine
   implementations? (Invariant 2.)
3. Who decides a pause? If the answer is "the Cog," the gate has moved to
   the wrong side of the seam.
4. Does the Track entry for a step carry the binding identity and, for gate
   decisions, the actor? If not, the Track cannot answer "what produced
   this?" or "who signed?"
5. Is the worker's return an envelope (`ok`, `payload`, `problems`,
   `binding`) rather than an ad hoc `{output, …}`?
6. Do credentials appear only by reference? Does the worker carry no
   user credential and no cluster credential it does not need?
7. Are `requires`/connector declarations still structured objects?
   (D9.)
8. Does an unknown or unavailable capability degrade uniformly rather than
   being special-cased? (Invariant 4.)

## How the open issues map to the ADR

| Issue | Decision / invariant | Note |
|---|---|---|
| #1 worker lifecycle on the hub | D5, invariants 1–3 | *Install* vs *materialize* — see vocabulary. Depends on #7 for the artifact to materialize from. |
| #2 durable Op engine | D2, D5, D8, invariant 5 | Gates are declared on the step; recovery must not depend on a caller re-submitting. |
| #3 model binding | D3, invariant 4 | The hub offers the inventory; the Cog's `resolve` selects; the binding record is the output. |
| #4 budgets and idle workers | invariant 3 | Duration is a hard pre-check; token/cost is post-interaction accounting. |
| #5 durable Track | D8 | Carry binding identity per step and actor per gate decision. Catalog persistence belongs to #7. |
| #6 least-privilege grant | invariant 2 | Namespace-scoped; workers carry no ServiceAccount token. |
| #7 registry and catalog | D6, D9 | The catalog card derives from the manifest; index the full profile as declared. |
| #8 delegated connector access | "deferred" list | The hub's brokered connectors act as the user; no credential enters the worker. |
| #9 Guards | seam item 2 | Guards consume the envelope; a Cog's contract checks are inputs to Guards, not Guards. Gates decide. |
| #10–#13 sensitivity | D9, D10, invariants 6–7 | See [sensitivity](sensitivity.md). Blocked on the rating scale. |
