# Archon: retained source evidence

Observed: 2026-10-15, with retry/loop corrections verified on 2026-10-16. Consolidated: 2026-10-18 under issue #95. Scope: source reading, not execution. Release snapshot: [v0.11.1, commit aa09544](https://github.com/coleam00/Archon/tree/aa09544); development snapshot: [54ec4fb](https://github.com/coleam00/Archon/tree/54ec4fb). Development observations must not be presented as released behavior. These facts have not been refreshed since the observation dates.

## Product and corrections

The reviewed product was a TypeScript/Bun workflow engine running coding-agent sessions and command nodes in user-defined YAML workflows. The earlier Python context/task-management server is a different, archived product; see the upstream [rewrite announcement](https://github.com/coleam00/Archon/issues/957). Archon therefore refuted the old survey's claim that no shipped system models this category.

The reviewed release supplied workflow execution, a web console, chat integrations and workflow packs. This is product evidence; kora's intended semantics do not establish superior readiness or security. At consolidation, kora has a packaging/frontend scaffold and schema tests; its execution platform remains unimplemented. Current kora behavior belongs to [ADRs](../../../docs/decisions/README.md) and [specifications](../../README.md), not to this comparison.

## Retry, loop and approval evidence

| Observation at the release snapshot | Source in upstream repository | Implication |
|---|---|---|
| Node retry attempts are recorded, including each attempt's output/error metadata. | [node-execution schema](https://github.com/coleam00/Archon/blob/aa09544/packages/workflows/src/schemas/node-execution.ts), [record serialization](https://github.com/coleam00/Archon/blob/aa09544/packages/workflows/src/node-record-serialization.ts) | Do not claim Archon has no attempt history. The relevant kora distinction is explicit prior-attempt inputs, artifact versions and lineage. |
| Loop iterations retain outputs, and `$LOOP_PREV` exposes the prior iteration. | [executor](https://github.com/coleam00/Archon/blob/aa09544/packages/workflows/src/dag-executor.ts), [artifact index](https://github.com/coleam00/Archon/blob/aa09544/packages/workflows/src/artifacts-index.ts) | Do not claim loops retain only the latest output or cannot feed back prior results. |
| Retry configuration and fatal-error handling are separate mechanisms. | [retry schema](https://github.com/coleam00/Archon/blob/aa09544/packages/workflows/src/schemas/retry.ts), original source comparison below | Avoid reducing all retries to “no conditions”; kora's declared error classes and nested retry semantics remain distinct. |

The development comparison recorded attention responses bound to node/pause identity; that was not evidence of binding approval to a candidate commit and gate digests. [ADR-0008](../../../docs/decisions/0008-content-bound-approvals.md) owns kora's required content binding. No runtime attack or bypass test was performed on either product.

## Boundaries and useful ideas

The source review distinguished a host git worktree from a sandbox. It found a Claude-specific container path for non-git use, so “host only” and “all sessions sandboxed” are both overstatements. Provider capability differences, authentication defaults and multi-user behavior must be read at the pinned snapshot; they are not current security assurances. Kora's [threat model](../../../docs/threat-model.md) records its own design controls and evidence limits.

Potential ideas retained from the comparison: explicit retry error classes, outcome fields for loop exit, typed human forms, trigger-rule vocabulary, provider-free dry runs, declared skills/MCP and reusable workflow packs. Existing requirements decide which are adopted; a research suggestion alone does not add scope. Chat-oriented operation, provider quotas and workflow-pack distribution remain distinct from kora's MVP commitments.

## Original evidence

The [full source comparison](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/archon-vs-kora.md) preserves per-row file/line references, release versus development labels, and the retry/loop correction trail. The [original product study](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/archon.md) preserves its bibliography. Popularity counts, release counts, pricing/licence speculation and numerical “wins” are retired from active documentation. Recheck upstream before using this note for a new product or dependency decision.
