# Architecture decision records

One record per significant decision, in the MADR shape: context, decision, alternatives considered, consequences, where it is applied. Records are numbered in the order they were written and never renumbered; a record that changes an earlier one says so in its header (`Supersedes` / `Superseded by`) and the earlier record is kept with its status updated. Reviewed by the project owner on 2026-10-16; the ADR-0016, ADR-0017, ADR-0018, ADR-0019 and ADR-0020 additions and amendments dated 2026-10-17 are pending owner review.

Ownership-index amendment dated 2026-10-18: scope approved under issue #95; wording pending owner review.

Slice-scope amendment dated 2026-10-19: ADR-0001 status updated under [issue #122](https://github.com/realkarych/kora/issues/122); wording pending line-by-line owner review.

Harness-scope amendment dated 2026-10-21: ADR-0001 requires both Claude and Codex in the first slice under [issue #172](https://github.com/realkarych/kora/issues/172); scope and implementation plan approved by the project owner, wording pending line-by-line owner review.

MCP long-operation amendment dated 2026-10-19: ADR-0002 and ADR-0020 statuses updated, and ADR-0014 and ADR-0017 carry dated tool-count amendments, under [issue #124](https://github.com/realkarych/kora/issues/124); wording pending line-by-line owner review.

Each record lists the identifiers it absorbs: `DD-n` from the flat register [knowledge/reviews/design-decisions-2026-10-15.md](../../knowledge/reviews/design-decisions-2026-10-15.md), which the specifications cite and which stays as the historical "where applied" index, and "requirements decision n" from section 13 of [requirements/functional.md](../requirements/functional.md). `DD-36` (implementation defaults) and `DD-37` (small mechanisms kept or dropped) point to their component specification owners through the register; it preserves identifiers and dispositions without overriding those current contracts.

| record | decision | status |
|---|---|---|
| [ADR-0001](0001-own-control-plane.md) | Own control plane, with a Temporal-backed execution backend kept possible | accepted, amended 2026-10-16, 2026-10-19 and 2026-10-21 |
| [ADR-0002](0002-ai-first-api-and-thin-clients.md) | AI-first: the HTTP API is the single implementation, every surface is a thin client, agent nodes never operate kora | accepted, amended 2026-10-16 and 2026-10-19 |
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
| [ADR-0019](0019-immutable-run-target-identity.md) | One immutable target per run; atomic acceptance of the complete target-source result; separate history and hold lifecycle | direction and plan accepted 2026-10-17; document pending owner review |
| [ADR-0020](0020-execution-capacity-and-bounded-run-acceptance.md) | Bounded outstanding run acceptance, physical execution-run occupancy and independent target holds; canonical start/replay and eligible ordering | direction and plan accepted 2026-10-17; amended by ADR-0002 2026-10-19; document pending owner review |
| [ADR-0021](0021-external-observations-outside-transactions.md) | Unlocked bounded external calls, exact current-owner application and recoverable approval delivery with cooperative providers | direction and plan accepted 2026-10-17; document pending owner review |
| [ADR-0022](0022-inherited-execution-deadlines.md) | Immutable inherited node/subgraph/run deadlines across managed phases; fixed nominal grace and evidence-based recovery | direction and plan accepted 2026-10-17; document pending owner review |
| [ADR-0023](0023-code-host-identity-and-purpose-scoped-credentials.md) | Code-host identity: credentials acquired per purpose by trusted code, GitHub App issuance in the control plane, honest backend reports, stable principals | accepted, wording pending review |
| [ADR-0024](0024-provider-limits-as-non-agent-failures.md) | Model-provider rate limits and quotas: a non-agent failure that waits for the reset, a pause per quota resource and limit group | accepted, wording pending review |
| [ADR-0025](0025-notifications.md) | Notifications: requested reviewers, durable outbound JSON, Slack and Telegram webhooks written with the event, subscriptions with a lifecycle | accepted, wording pending review |
| [ADR-0026](0026-package-layers.md) | Package layers: packages by role under `src/kora`, server-free packages never statically reach server packages, a standard-library test enforces it, one base installation | accepted, wording pending review |

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

[ADR-0022](0022-inherited-execution-deadlines.md) defines one inherited node/subgraph/run deadline across managed phases, fixed nominal grace and unchanged termination-evidence barriers. Direction and implementation plan accepted on 2026-10-17; document wording pending owner review.

[ADR-0023](0023-code-host-identity-and-purpose-scoped-credentials.md) defines kora's identity on code hosts: credentials acquired per purpose by trusted code at the moment they are needed, GitHub App issuance in the control plane, backend reports of the rights actually enforced, and stable principals for automation and self-approval checks. Direction and implementation plan accepted on 2026-10-18, relayed from the project owner; document wording pending owner review.

[ADR-0024](0024-provider-limits-as-non-agent-failures.md) makes model-provider rate limits and quotas a non-agent failure: `provider_limited`, a time-bounded `provider_retry` policy, a wait until the reset, and one admission pause per quota resource and limit group. Direction and implementation plan accepted on 2026-10-18, relayed from the project owner; document wording pending owner review.

[ADR-0025](0025-notifications.md) defines notifications: reviewer requests through the VCS integration and durable at-least-once outbound webhooks (signed JSON, Slack, Telegram) whose delivery records are written with the event. Direction and implementation plan accepted on 2026-10-18, relayed from the project owner; document wording pending owner review.

[ADR-0026](0026-package-layers.md) places every shared component in a package by role under `src/kora` and fixes which package may import which. The server-free packages, such as the runner, the worker, the client and the DSL, never reach the server packages through static imports, and only the CEL evaluator imports the binding. A standard-library pytest test enforces this in CI. Layering is a packaging rule with defence in depth, not an isolation control.

[ADR-0027](0027-bounded-effective-schema-validation.md) selects common core composition and a restricted in-process StructuredOutput schema-execution architecture. It preserves standalone provider hooks, records explicit unsupported schemas and the prerequisite for a complete immutable capacity/accounting policy, and leaves evaluator and consumer implementation to separate work. The project owner approved this record and its exact wording on 2026-10-30 under issue #138.
