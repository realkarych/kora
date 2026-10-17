# kora knowledge base

Reference material for engineers and coding agents working on kora. Start with the [technical introduction](../docs/technical-introduction.md) for a short explanation of the core. Then load the exact specifications for the component being changed. Use the [architecture reference](architecture/detailed.md) for component views, deployment and module maps, the stack, canonical terms and open implementation questions. Files here may be produced by agents and are not necessarily reviewed line by line by a person; where anything here disagrees with the owner decisions in `docs/`, those decisions win and this folder is fixed. The human-facing documentation (positioning, technical introduction, requirements, decision records, proposals) is in [`docs/`](../docs/README.md), where each document records its review status; the files here apply its decisions.

## Which file to load for which task

| task | load |
|---|---|
| Implement or change the graph document format, its schema or the compiler | [specs/ir.md](specs/ir.md), [specs/ir.schema.json](specs/ir.schema.json), [specs/examples/](specs/examples/), [specs/dsl.md](specs/dsl.md) |
| Write or change Python graph code, presets, the compile step | [specs/dsl.md](specs/dsl.md), then [specs/ir.md](specs/ir.md) for what it compiles to |
| Add or change a table, a state machine, retention, lineage | [specs/data-model.md](specs/data-model.md) |
| Scheduling passes, leases, waits, timeouts, retries, concurrency, unlocked external observation/delivery, standalone mode, migrations, the operability runbook | [specs/scheduler.md](specs/scheduler.md) |
| Implement a provider (runtime, harness adapter, VCS, approval channel, secrets, artifact storage, artifact type, observability exporter, authentication, sensor, policy) or its conformance suite; the harness mapping tables | [specs/protocols.md](specs/protocols.md) |
| Any HTTP endpoint, CLI command, MCP tool, harness skill or web console view, and the parity rules between them | [specs/api-and-agent-surface.md](specs/api-and-agent-surface.md) |
| Build or change a web console page: the elements of every page and the routes behind them, the state-to-colour mapping, the two pilot scenarios | [design/console/pages.md](design/console/pages.md), [design/console/states.md](design/console/states.md), [design/console/tokens.md](design/console/tokens.md), [design/console/pilot-scenarios.md](design/console/pilot-scenarios.md); the decisions are [docs/console.md](../docs/console.md) and [ADR-0015](../docs/decisions/0015-web-console-design.md) |
| Understand the core framework: components, connections and one run through the system | [docs/technical-introduction.md](../docs/technical-introduction.md) |
| Locate a component and its contracts before a contribution | [Module and contract map](architecture/detailed.md#module-and-contract-map), then the component specification and its decision records |
| Component views, protected execution, deployment shapes, the stack, canonical terms and unresolved implementation questions | [architecture/detailed.md](architecture/detailed.md); [open implementation questions](architecture/detailed.md#open-implementation-questions) |
| Why a decision was taken, what was rejected | [docs/decisions/](../docs/decisions/README.md) first; the flat register of design decisions with "where applied" pointers is [reviews/design-decisions-2026-10-15.md](reviews/design-decisions-2026-10-15.md) |
| What the design reviews found and how each finding was dispositioned | [reviews/review-2026-10-15.md](reviews/review-2026-10-15.md) |
| The architecture critique before implementation (execution authority, evidence, trust boundaries, versioning, cost, competitors, engine choice) with the owner's disposition and the issue for each finding | [reviews/architecture-critique-2026-10-16.md](reviews/architecture-critique-2026-10-16.md) |
| Retry invalidation of unbound dependents, input independence and wrapper recollection | [reviews/retry-dependency-invalidation-review-2026-10-17.md](reviews/retry-dependency-invalidation-review-2026-10-17.md), then [specs/data-model.md](specs/data-model.md#6-clear-downstream-fr-68a) |
| Branch target ownership, retained selection and routing after retry | [reviews/branch-target-gating-review-2026-10-17.md](reviews/branch-target-gating-review-2026-10-17.md), then [specs/ir.md](specs/ir.md#45-branch-ownership-and-selection) |
| Durable readiness discovery across commits, round limits and restart | [reviews/durable-readiness-discovery-review-2026-10-17.md](reviews/durable-readiness-discovery-review-2026-10-17.md), then [specs/scheduler.md](specs/scheduler.md#8-restart-and-multi-scheduler-safety) |
| Whether to build on Temporal, Kestra, Airflow or DBOS instead of an own scheduler, requirement by requirement | [analysis/own-scheduler-vs-existing-engine/verdict.md](analysis/own-scheduler-vs-existing-engine/verdict.md) and the per-engine files beside it |
| Competitors and adjacent systems (Archon, vendor cloud sessions, agent-graph frameworks, classic orchestrators, runtimes and sandboxes, cost accounting options, declarative specifications, durable execution and observability) | [research/agentic-pipeline-orchestrators.md](research/agentic-pipeline-orchestrators.md) (report), then the notes in [research/agentic-pipeline-orchestrators/](research/agentic-pipeline-orchestrators/) |
| Archon compared with kora aspect by aspect, with the Archon file and the kora requirement or decision behind every row | [research/agentic-pipeline-orchestrators/archon-vs-kora.md](research/agentic-pipeline-orchestrators/archon-vs-kora.md) |
| What other orchestrators' consoles show first, how they colour states, how approval forms and agent trace viewers are laid out; dated | [research/orchestrator-consoles.md](research/orchestrator-consoles.md) |
| Through which mechanism each harness lets the session runner intercept and deny a tool call, with the vendor sources and the date checked | [research/harness-policy-hooks.md](research/harness-policy-hooks.md) |

For external-call ownership and cooperative-provider limits, read [ADR-0021](../docs/decisions/0021-external-observations-outside-transactions.md), [data-model section 14](specs/data-model.md#14-external-observation-and-delivery-ownership-adr-0021), [scheduler section 16](specs/scheduler.md#16-external-observation-dispatch-adr-0021), the [design review](reviews/external-observation-design-review-2026-10-17.md) and [dated failure-model evidence](research/cooperative-provider-failure-model.md).

## Folders

- `specs/`: normative specifications of the MVP design. They cite requirements (`FR-n.m`, `NFR-n.m`) and decisions (`ADR-000n`, `DD-n`) and are the source for the implementation; when a specification and a decision record disagree, the decision record wins.
- `design/`: the detailed design of a user-facing surface behind a page in `docs/` (today the web console): element lists, state mappings and scenarios, never decisions.
- `architecture/`: reference views and tables behind the [technical introduction](../docs/technical-introduction.md), with a module-to-contract map and unresolved implementation questions. Exact behavior belongs to the specifications.
- `reviews/`: dated review records and the flat register of design decisions (`DD-n`) that the specifications cite. The register is historical; its decisions are grouped and explained in `docs/decisions/`.
- `analysis/`: desk analyses that answered a specific question; each states its method and date.
- `research/`: competitor and market research with sources; dated, not maintained after the fact.

For inherited execution deadlines, read [ADR-0022](../docs/decisions/0022-inherited-execution-deadlines.md), [data-model section 15](specs/data-model.md#15-inherited-execution-deadlines-adr-0022), [scheduler section 17](specs/scheduler.md#17-inherited-deadline-enforcement-adr-0022), runtime lifecycle/conformance in [protocols](specs/protocols.md), the [dated clock evidence](research/execution-deadline-clock-model.md) and [design/review record](reviews/inherited-execution-deadline-review-2026-10-17.md).
