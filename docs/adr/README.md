# Architecture decision records

Decisions that shape Collab Hub and are expensive to reverse are recorded
here as ADRs, one file per decision set, numbered in the order they were
opened. An ADR states the context, the decisions, the invariants reviewers
should enforce, and what was deliberately left out. It is not a design
doc — it records *what was decided and why*, so later work can be checked
against it.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-cog-execution.md) | Cog & Op execution architecture | Proposed |

**Statuses.** *Proposed* — written, under review, already the working basis
for implementation. *Accepted* — ratified; changes require a superseding
ADR. *Superseded* — replaced; the record stays for history.

**Citing an ADR.** Issues and code comments reference decisions and
invariants by number — "ADR-0001 D5", "ADR-0001 invariant 2" — so keep
those numbers stable. Add new decisions at the end; never renumber.
