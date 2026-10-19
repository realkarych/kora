# Orchestration research: evidence and disposition index

Observed: 2026-10-15–16. Consolidated: 2026-10-18 under issue #95. This is historical desk research, not a fresh survey, runtime evaluation or source of decisions. External releases, prices and capabilities must be rechecked before a new selection. Current requirements and ADRs take precedence.

## Conclusions retained

- The original claim that no shipping product models a user-defined graph of coding-agent sessions was withdrawn. Archon is a direct neighbour; see the [dated source evidence](agentic-pipeline-orchestrators/archon.md) and [human-facing comparison](../../docs/archon-comparison.md).
- Classic orchestrators do not uniformly reduce agents to a model-call loop. The original correction identified Airflow's managed-session operator. It also distinguished Kestra subflow retry from whole-group retry available only in a later development change. These observations refute blanket absence claims; they do not establish equivalence with kora's attempt, artifact or approval semantics.
- An own scheduler is a choice, not a demonstrated necessity. [Engine evidence](../analysis/own-scheduler-vs-existing-engine/verdict.md) retains the Temporal and DBOS alternatives, corrected Airflow/Kestra limitations and the absence of comparative measurements. [ADR-0001](../../docs/decisions/0001-own-control-plane.md) owns the decision and the scheduler-prototype reconsideration gate.
- The product hypotheses concern organisation-level operations, content-bound approvals, retry lineage and replaceable runtime/VCS infrastructure. The [positioning](../../docs/positioning.md) and [requirements](../../docs/requirements/functional.md) define the intended product; survey coverage cannot prove uniqueness or demand.
- Harness limits and enforcement vary by adapter. Use [dated hook evidence](harness-policy-hooks.md) and [protocol contracts](../specs/protocols.md), not old CLI flags, SDK versions, vendor pricing or capability counts copied from a broad survey.

## Source and disposition inventory

The linked Git snapshots preserve the complete reports, cited external sources and original corrections. They are historical evidence loaded on demand, not current instructions. No accepted requirement is owned by these reports.

| Original body | Immutable source | Current disposition |
|---|---|---|
| Umbrella survey | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators.md) | Replaced by this index; the contradicted headline and unverified novelty claims are withdrawn. |
| Agent graph frameworks | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/agent_graph_frameworks.md) | No framework is adopted; current DSL and IR requirements own the design. |
| Classic orchestrators | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/classic_orchestrators.md) | Corrected engine trade-offs are retained in the engine evidence below. |
| Coding-agent orchestration | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/coding_agent_orchestration.md) | The market-exclusivity claim was withdrawn; Archon evidence is retained below. |
| Cost accounting | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/cost_accounting_options.md) | ADR-0010 and the protocols own the ledger and soft-budget contract; historical price tables are retired. |
| Declarative specifications | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/declarative_specs.md) | ADR-0002/0006 and DSL/IR own authoring, validation and preset semantics. |
| Durability and observability | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/durable_execution_observability.md) | ADR-0001/0003 and scheduler/protocol specifications own the current contracts. |
| Market positioning | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/market_positioning_and_gaps.md) | Broad novelty, popularity and market-size claims are not maintained as evidence of a product gap. |
| Runtimes and sandboxes | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/runtimes_and_sandboxes.md) | ADR-0004/0005/0009 and protocols own execution authority; focused harness-policy research remains active. |
| Archon product research | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/archon.md) | Compact dated evidence retained; popularity counts, commercial speculation and repeated product matrices retired. |
| Detailed Archon comparison | [Original](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators/archon-vs-kora.md) | Source-level retry/loop corrections retained in the compact note; current kora behavior belongs to its ADRs/specifications. |

The engine note links each original per-engine classification. The [load estimate](../analysis/control-plane-load-estimate.md) retains assumptions with corrected arithmetic and explicitly withdraws unmeasured capacity/headroom conclusions.

## Evidence still needed

No supplier source review establishes kora throughput, security enforcement, recovery correctness or usability. The [scheduler-prototype acceptance checks](../specs/scheduler.md#18-scheduler-prototype-milestone-acceptance), [NFRs](../../docs/requirements/non-functional.md) and [verification coverage](../reviews/ai-development-setup.md) define the evidence to collect. Revisit a competitor or engine only for a concrete open decision; date that new evidence rather than extending this survey indefinitely.
