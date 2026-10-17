# Architecture decision records

One record per significant decision, in the MADR shape: context, decision, alternatives considered, consequences, where it is applied. Records are numbered in the order they were written and never renumbered; a record that changes an earlier one says so in its header (`Supersedes` / `Superseded by`) and the earlier record is kept with its status updated. Reviewed by the project owner on 2026-10-16; the ADR-0016, ADR-0017 and ADR-0018 additions and amendments dated 2026-10-17 are pending owner review.

Each record lists the identifiers it absorbs: `DD-n` from the flat register [knowledge/reviews/design-decisions-2026-10-15.md](../../knowledge/reviews/design-decisions-2026-10-15.md), which the specifications cite and which stays as the historical "where applied" index, and "requirements decision n" from section 13 of [requirements/functional.md](../requirements/functional.md). `DD-36` (first-slice implementation defaults) and `DD-37` (small mechanisms kept or dropped) are implementation-level and stay only in the register.

| record | decision | status |
|---|---|---|
| [ADR-0001](0001-own-control-plane.md) | Own control plane, with a Temporal-backed execution backend kept possible | accepted, amended 2026-10-16 |
| [ADR-0002](0002-ai-first-api-and-thin-clients.md) | AI-first: the HTTP API is the single implementation, every surface is a thin client, agent nodes never operate kora | accepted |
| [ADR-0003](0003-scheduler-and-workers-over-http.md) | Scheduler and workers are separate processes; workers reach the control plane only through the HTTP API | accepted |
| [ADR-0004](0004-harness-adapters-over-vendor-sdks.md) | Harness adapters drive harnesses through vendor interfaces (Claude Agent SDK, Codex app-server; an ACP adapter after MVP), not CLI output; harness versions are fixed per installation; kora enforces the limits | accepted, amended 2026-10-16 |
| [ADR-0005](0005-eleven-protocols-with-conformance-suites.md) | Eleven provider protocols, each with a conformance suite, registered through one entry point | accepted |
| [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md) | Graphs are written in a Python DSL and compiled to a versioned YAML intermediate representation | accepted |
| [ADR-0007](0007-attempt-model-and-retries.md) | Explicit attempts with lineage, sub-graph iteration semantics, at-most-once submission and retention | accepted |
| [ADR-0008](0008-content-bound-approvals.md) | Approvals bind to content (commit SHA plus ReviewBundle fact digests); VCS approvals must come from a real reviewer | accepted |
| [ADR-0009](0009-target-repository-trust-boundary.md) | The target repository is untrusted by default: `trust_repo_config`, protected paths and tool policy | accepted |
| [ADR-0010](0010-cost-accounting-and-limits.md) | Cost is a token ledger with a pluggable price table; USD limits are soft, hard limits are timeouts and event counts | accepted |
| [ADR-0011](0011-namespaces-and-personal-tokens.md) | Namespaces and personal tokens with two flags; no roles in the MVP | accepted |
| [ADR-0012](0012-no-transform-node.md) | No transform node kind; deterministic steps are command nodes, the narrative is an agent node, kora attaches cost | accepted, amended 2026-10-16 |
| [ADR-0013](0013-implementation-stack.md) | Implementation stack | accepted |
| [ADR-0014](0014-target-identity.md) | Target identity above runs: an optional `target_key` with namespace scope, a per-target admission policy `allow` or `defer`, the target's history as a projection over runs | accepted |
| [ADR-0015](0015-web-console-design.md) | The web console's design: the graph as the primary view, four actions in MVP, one state vocabulary, and the data the API exposes for it | accepted |
| [ADR-0016](0016-trusted-execution-and-publication.md) | Trusted lifecycle control and separate late publication; candidate commit history preserved; local trusted for development/tests | direction accepted 2026-10-17; document pending owner review |
| [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md) | Preserve execution uncertainty; require runtime termination and dispatcher closure before replacement; explicit operator reconciliation | direction and plan accepted 2026-10-17; document pending owner review |

| [ADR-0018](0018-immutable-attempt-history.md) | Immutable deployment contracts and attempt-owned inputs/requests; explicit compatibility, retention and access boundaries | direction and plan accepted 2026-10-17; document pending owner review |

## Writing a new record

Copy the skeleton below into `NNNN-short-title.md` with the next free number, fill every section, add the row above, and link the record from the specification sections that apply it.

```markdown
# ADR-NNNN: <title>

- Status: proposed | accepted, <date> | superseded by ADR-MMMM
- Reviewed: by the project owner, <date>
- Deciders: project owner
- Absorbs: <identifiers, if any>
- Related: <other records>

## Context
## Decision
## Alternatives considered
## Consequences
## Where it is applied
```
