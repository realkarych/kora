# kora and Archon

Reviewed by the project owner on 2026-10-16. Consolidation amendment dated 2026-10-18: scope approved under issue #95; wording pending line-by-line owner review.

Archon is a close neighbour: a workflow engine whose nodes include full coding-agent sessions. The [dated research note](../knowledge/research/agentic-pipeline-orchestrators/archon.md) records the release and development snapshots, source-level corrections and original comparison. This page describes the product distinction; external capability details belong to that evidence, not to a continuously growing feature scoreboard.

## Product distinction

| Concern | Evidence from the Archon review | kora's intended answer and current owner |
|---|---|---|
| Readiness and everyday use | An executable product with workflows, console, chat integrations and packs | A scaffold exists; execution semantics remain a design. The [roadmap](roadmap.md) defines the implementation sequence and evidence still needed. |
| Operator and deployment | Repository-oriented workflows for one operator or a small team | Organisation-level control plane with outbound workers and replaceable infrastructure: [positioning](positioning.md), [ADR-0003](decisions/0003-scheduler-and-workers-over-http.md), [ADR-0005](decisions/0005-eleven-protocols-with-conformance-suites.md). |
| Attempts and feedback | Retry attempts and loop iteration outputs are recorded; previous-loop feedback exists | Explicit attempt inputs, artifact versions and lineage: [ADR-0007](decisions/0007-attempt-model-and-retries.md). The distinction is not the mere existence of history. |
| Approval authority | The reviewed gate mechanisms did not establish candidate-content binding | Approval binds to the commit and gate facts under [ADR-0008](decisions/0008-content-bound-approvals.md); later publication/recovery decisions constrain its use. |
| Execution and trust | A worktree is not itself a sandbox; provider-specific isolation and permission mechanisms differ | Hermetic defaults and replaceable runtime/VCS protocols are required design controls. The [threat model](threat-model.md) distinguishes those requirements from demonstrated enforcement. |
| Cost, capacity and recovery | Useful existing product mechanisms, including provider-aware behavior | [ADR-0010](decisions/0010-cost-accounting-and-limits.md), [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md) and [ADR-0020](decisions/0020-execution-capacity-and-bounded-run-acceptance.md) define kora's contracts and their limits. They are not measured advantages. |
| User experience | Ready-made workflows, chat and provider-free workflow testing informed the review | The full MVP console and pilot scenarios are specified in [console.md](console.md). Chat operation and workflow-pack distribution are not silently added to MVP. |

## Implications

A design matrix cannot establish that kora is stronger than an implemented product. The earlier numerical win count is retired. kora must demonstrate its intended advantages in retry lineage, content binding, recovery and infrastructure integration, while meeting the usability bar set by existing tools. Its scale target and security guarantees remain acceptance obligations, not comparative results.

Graphs reuse ordinary Python imports and carry digest-pinned command resources for use against another repository, as specified in the [DSL](../knowledge/specs/dsl.md). This does not introduce a workflow catalogue.

The useful ideas and full historical matrix remain linked from the research note. New comparisons should answer a concrete product decision with fresh dated evidence. Current kora behavior is maintained in its requirements, ADRs and component specifications.
