# Cog execution — read this first

This directory is the basis for the hub's Cog and Op execution work: the
vocabulary, the boundary between the hub and a Cog, and the contracts that
cross it. Read it before picking up any issue labeled `cog-execution`, and
check work against it in review. The decisions themselves are recorded in
[ADR-0001](../adr/0001-cog-execution.md); this directory explains the terms
and the seam the ADR assumes.

Contents:

- **This page** — vocabulary, the seam in one paragraph, what stays inside a
  Cog, a review checklist, and how the open issues map to the ADR.
- [The Op–Cog seam](op-cog-seam.md) — the four things an Op step needs from
  a Cog, and why nothing else crosses.
- [The result envelope](result-envelope.md) — the shape a Cog's entry point
  returns; what Guards check, Gates read, and Tracks record.
- [Sensitivity](sensitivity.md) — how data-sensitivity labels are born,
  propagated, and enforced (the basis of ADR-0001 D10).

## Vocabulary

These terms are used precisely throughout the issues, the ADR, and the
code. When a word here has a narrower meaning than its everyday one, the
narrower meaning is the one intended.

**Cog.** A packaged, installable AI worker. A *complete* Cog is the model,
the weights, the model runtime (including any harness), and the context. A
Cog is described by its manifest (`cog.yaml`, following the shared Cog
specification as a profile) and runs itself: it carries its own pinned
environment and its own entry points. The hub never supplies a Cog's
runtime.

**Cog classes.** Declared in the manifest, not separate code paths:
*model* Cogs serve a model (or describe one the environment serves — a
*descriptor* Cog); *harness* Cogs supply interaction machinery; *context*
Cogs carry prompts, schemas, and frames and point at a model rather than
carrying one; *complete* Cogs carry all three. Classes compose through
`provides`/`requires` (ADR-0001 D3).

**Entry point.** A declared, invokable interface of a Cog. Two audiences:
*usage* entry points (`ask`, `chat`, …) face end users and the Op layer;
*lifecycle* entry points (`resolve`, `serve`, `check`, `eval`, …) face the
hosting environment. A Cog's usage entry points are its **bundled ops** —
single-Cog, zero-gate operations shipped in the package.

**Op.** A supervised, multi-step workflow that a person kicks off. Ops
compose Cogs' usage entry points into steps and wrap Frames, Guards,
Gates, and Tracks around them. "Run a Cog once" and "a five-step proposal
with human sign-off" are the same concept at two scales.

**Hosts / invokes.** The hub *hosts* a Cog by invoking its lifecycle entry
points (install, resolve, serve, check) and *invokes* a Cog by calling its
usage entry points. "Run a Cog" in orchestrator code should always mean one
of those two verbs — never "execute the Cog's internals."

**Materialize / worker.** To materialize a Cog is to bring up a running
instance of its `serve` entry point where the hub can reach it — on
Kubernetes, a pod behind a Service. That running instance is a **worker**.
Materialization happens behind the executor interface (ADR-0001 D5,
invariant 2); teardown reclaims it. *Install* is earlier and separate:
fetching the package, provisioning its environment, running its checks,
resolving its requirements, recording the binding, and deriving its
catalog card. Installing makes a Cog *invokable*; the first invocation
materializes a worker.

**Satisfier / resolution / binding record.** A Cog declares what it
`requires` (a model capability, a harness, a connector). A *satisfier* is
something in the environment that `provides` it. *Resolution* is the Cog's
own `resolve` lifecycle entry point selecting satisfiers from the inventory
the hosting environment offers. The result is the **binding record**: which
pinned model, which endpoint, which harness, which evidence — the identity
that every result and every Track entry carries. Credentials appear in a
binding only by reference, never as values.

**Locality.** Where a satisfier runs and where data goes when it is used —
in-cluster, on-prem, or an external provider. A bind-time constraint that
resolution enforces so data stays where policy allows; the model
[sensitivity](sensitivity.md) generalizes.

**Nebi.** The package manager for Cogs (ADR-0001 D6): `nebi pull` installs a
Cog's files, `nebi run` executes one of its entry points, and publishing
goes through Nebi to an OCI registry the hub indexes.

**Capability list.** What a hosting environment publishes: the satisfiers it
offers and the requirements it places on Cogs that run there (envelope
shape, io values, entry-point forms). Environment-owned; needs no
ratification in the Cog spec.

**Frame.** Organizational context supplied to a step — saved Markdown
context, org policy, the material a Cog is asked to work from.

**Contract check.** A Cog's own in-package validation of its declared
contract (schema, grounding, citation, identity). Self-reported in the
envelope's `problems` list. *Never* call these Guards.

**Guard.** An independent, system-side verifier of the *system's*
requirements, run by the Op layer on a step's result envelope. A Guard
produces findings; it does not decide.

**Gate.** The decision point on a step: ok / ok-with-problems / escalate to
a human. Gates consume the envelope (payload and problems) and Guard
findings. Gates decide; Cogs never do. A gate is declared on the Op step,
not implemented inside the Cog.

**Track.** The durable, append-only record of a run: which Cogs ran under
which bindings, which Guards and Gates fired, who approved, what came out.
Status is derived from the Track, never held only in memory. The Track is
the accountability record — a Gate signature is only meaningful because
the Track can say *which* model, *which* weights, *which* evidence produced
the thing that was signed.

**Result envelope.** The structured return of a usage entry point: `ok`,
`payload`, `problems`, `binding`, plus usage and timing. See
[the envelope](result-envelope.md).

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
