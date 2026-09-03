# Glossary

The vocabulary used across Collab Hub's issues, ADRs, and docs, defined in
one place. When a word here has a narrower meaning than its everyday one,
the narrower meaning is the one intended. Every entry cites where its
definition comes from; when a new ADR introduces a term, its entry is added
here in the same PR.

Two tiers. **Platform concepts** come from the public
[Intelligence Hub whitepaper](https://github.com/openteams-ai/inthub-whitepaper/blob/main/whitepaper.md),
which is the authority on what they mean; where the hub's usage is narrower
than the whitepaper's, the entry says so. **Hub execution vocabulary** is
defined by [ADR-0001](adr/0001-cog-execution.md) and the
[cog-execution docs](cog-execution/README.md) — these terms do not appear
in the whitepaper and this glossary is their home.

## Platform concepts (defined by the whitepaper)

**Cog.** A packaged, installable AI worker (whitepaper §4.4: "a unit that
can be installed and replaced"). A *complete* Cog is the model, the
weights, the model runtime (including any harness), and the context. A Cog
is described by its manifest (`cog.yaml`, following the shared Cog
specification as a profile) and runs itself: it carries its own pinned
environment and its own entry points. The hub never supplies a Cog's
runtime. The whitepaper also describes governance parameters a Cog
carries; in the hub those are enforced by the environment (Guards, Gates,
the capability list), not by the Cog's own code.

**Op.** A supervised, multi-step workflow that a person kicks off
(whitepaper §4.5: "a supervising model that may coordinate Cog execution…
and human-in-the-loop feedback points"). Ops compose Cogs' usage entry
points into steps and wrap Frames, Guards, Gates, and Tracks around them.
"Run a Cog once" and "a five-step proposal with human sign-off" are the
same concept at two scales.

**Frame.** Organizational context supplied to a step — saved Markdown
context, org policy, the material a Cog is asked to work from. The hub's
Frames API is the service that stores and serves them.

**Guard.** An independent, system-side verifier of the *system's*
requirements, run by the Op layer on a step's result envelope (whitepaper
§5.1 names Schema, Source, Policy, and Privacy Guards). A Guard produces
findings; it does not decide. A Cog's own self-validation is a *contract
check*, not a Guard — see below.

**Gate.** The decision point on a step: ok / ok-with-problems / escalate
to a human (whitepaper §5). Gates consume the envelope (payload and
problems) and Guard findings. Gates decide; Cogs never do. A gate is
declared on the Op step, not implemented inside the Cog.

**Track.** The durable, append-only record of a run: which Cogs ran under
which bindings, which Guards and Gates fired, who approved, what came out
(whitepaper §5.3). Status is derived from the Track, never held only in
memory. The Track is the accountability record — a Gate signature is only
meaningful because the Track can say *which* model, *which* weights,
*which* evidence produced the thing that was signed.

## Hub execution vocabulary (defined by ADR-0001 and the cog-execution docs)

**A4.** The current delivery milestone. ADR-0001 scopes several decisions
"for A4"; it is a schedule boundary, not an architecture concept.

**Binding record.** The output of resolution: which pinned model, which
endpoint, which harness, which evidence — the identity that every result
envelope and every Track entry carries. Credentials appear in a binding
only by reference, never as values. (ADR-0001 D8; seam note.)

**Bundled op.** A usage entry point shipped in a Cog's package: a
single-Cog, zero-gate op (`ask`, `chat`, build-a-bundle-and-ask). Bundled
ops appear in the catalog card. Lifecycle entry points are *not* bundled
ops. (Seam note, "Bundled ops".)

**Capability list.** What a hosting environment publishes: the satisfiers
it offers and the requirements it places on Cogs that run there (envelope
shape, io values, entry-point forms). Environment-owned; needs no
ratification in the Cog spec. (Seam note, "Who builds what".)

**Catalog card.** A Cog's entry in the catalog — name, function, io, and
its usage entry points — derived from the manifest, never a hub-invented
schema. How Cogs appear in the Op builder's picker. (Seam item 4; ADR-0001
D9.)

**Cog classes.** Declared in the manifest, not separate code paths:
*model* Cogs serve a model (or describe one the environment serves — a
*descriptor* Cog); *harness* Cogs supply interaction machinery; *context*
Cogs carry prompts, schemas, and frames and point at a model rather than
carrying one; *complete* Cogs carry all three. Classes compose through
`provides`/`requires`. (ADR-0001 D3.)

**Contract check.** A Cog's own in-package validation of its declared
contract (schema, grounding, citation, identity), self-reported in the
envelope's `problems` list. Input to Guards, never a Guard's verdict —
and never called a Guard. (Envelope doc, `problems`.)

**Data catalog.** The environment service that rates data sources on the
shared sensitivity scale. Labels are born on data, in the catalog — not
declared by Cogs. (ADR-0001 D10; sensitivity doc.)

**Engine (orchestration engine).** The component that runs an Op's steps
durably: submit, signal a paused step, observe state. Sits behind its own
interface so the implementation is swappable; distinct from the executor.
(ADR-0001 D5; issue #2.)

**Entry point.** A declared, invokable interface of a Cog. Two audiences:
*usage* entry points (`ask`, `chat`, …) face end users and the Op layer;
*lifecycle* entry points (`resolve`, `serve`, `check`, `eval`, …) face the
hosting environment. (Cog-execution README; seam note.)

**Executor.** The component that materializes and tears down Cog workers
on some substrate (Kubernetes is the default implementation). Pluggable;
no raw cluster primitives leak past it into orchestration. (ADR-0001 D5,
invariant 2.)

**Hosting environment.** The place a Cog is installed and run — the hub,
or later a desktop. It owns the lifecycle entry points, offers the
satisfier inventory, and publishes its capability list. (Seam note, "Who
builds what".)

**Hosts / invokes.** The two verbs of "running a Cog": the environment
*hosts* a Cog by invoking its lifecycle entry points (install, resolve,
serve, check) and *invokes* it by calling its usage entry points.
Orchestrator code never executes a Cog's internals. (ADR-0001 D2; seam
note.)

**Install.** Making a Cog invokable: fetch the package, provision its
environment, run its checks, resolve its requirements, record the binding,
derive its catalog card. Distinct from materialize — installing does not
start a worker. (Cog-execution README, "Materialize / worker".)

**Locality.** Where a satisfier runs and where data goes when it is used —
in-cluster, on-prem, or an external provider. A bind-time constraint that
resolution enforces so data stays where policy allows; the sensitivity
model generalizes it. (Sensitivity doc.)

**Materialize / worker.** To materialize a Cog is to bring up a running
instance of its `serve` entry point where the hub can reach it — on
Kubernetes, a pod behind a Service. That running instance is a **worker**.
Materialization happens behind the executor; teardown reclaims it. The
first invocation of an installed Cog materializes a worker. (ADR-0001 D5;
cog-execution README.)

**Nebi.** The package manager for Cogs: `nebi pull` installs a Cog's
files, `nebi run` executes one of its entry points, and publishing goes
through Nebi to an OCI registry the hub indexes. (ADR-0001 D6.)

**Result envelope.** The structured return of a usage entry point: `ok`,
`payload`, `problems`, `binding`, plus usage and timing — exactly what
Guards check, Gates read, and Tracks record. (The
[envelope doc](cog-execution/result-envelope.md).)

**Satisfier / resolution.** A Cog declares what it `requires` (a model
capability, a harness, a connector); a *satisfier* is something in the
environment that `provides` it. *Resolution* is the Cog's own `resolve`
lifecycle entry point selecting satisfiers from the inventory the hosting
environment offers; the result is the binding record. (ADR-0001 D3,
invariant 4; cog-execution README.)

**Sensitivity label / effective label.** A rating a data source carries in
the data catalog, on a shared multi-axis scale. A run's *effective* label
is the strictest value on each axis across the sources actually bound; a
step's output label is at least the strictest of its inputs, absent a
declared, Guard-verified, Gate-signed downgrade. (ADR-0001 D10, invariants
6–7; sensitivity doc.)
