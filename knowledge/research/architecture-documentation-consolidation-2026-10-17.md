# Architecture documentation consolidation audit

Checked: 2026-10-17. Baseline: `1baf9891af1d8dc69e4e33dd7bc2af22237072c5`. Status: editorial audit for [issue #76](https://github.com/realkarych/kora/issues/76), not an architecture decision or completed validation record.

The consolidation removes `docs/architecture.md` and `knowledge/architecture/internals.md`, retaining [detailed.md](../architecture/detailed.md) as a compact reference. The [technical introduction](../../docs/technical-introduction.md) owns orientation, the core map and one scoped successful run. Specifications own behavior; ADRs own decisions. The reference preserves four complementary diagrams, module/contract navigation, the dependency inventory, one glossary and thirteen unresolved questions. Historical evidence survives through immutable citations. The repository remains design-only; diagrams and future conformance gates are not implementation evidence. ([Baseline introduction](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/technical-introduction.md), [project status](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/roadmap.md#where-the-project-stands))

## Disposition of repeated explanation

All overview sections are removed: status/amendments, purpose, context, containers, stack, boundaries and onward reading. Purpose remains in positioning; explanations belong to the introduction, while stack detail remains in the reference. The overview’s reviewed-status claim does not apply to rewritten material. ([Overview](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/architecture.md))

The following maps all **20 chapters and three appendices**. “Removed” refers to repeated explanation; normative sources remain. ([Book](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md))

| Chapter or appendix | Disposition and surviving owner |
|---|---|
| 1. What kora is | Removed; introduction, IR example. |
| 2. The domain model | Removed; data-model specification; no additional ER overview. |
| 3. Processes and where they run | Removed; scheduler §1/10, deployment and protected-boundary views. |
| 4. The plug-in seam | Removed; protocols, ADR-0005 and contract navigation. |
| 5. Code map | Planned areas and contract dependencies retained as navigation. |
| 6. Invariants | Removed; owning ADRs/specifications; no replacement register. |
| 7. One run, end to end | Removed; introduction’s walkthrough. |
| 8. One attempt, and when a process dies | Removed; scheduler §3 and runtime/VCS contracts. |
| 9. Waits, approvals and retries | Removed; data-model/scheduler/IR; run-outcome question retained. |
| 10. Limits, cost and safe retries | Removed; IR/data-model/scheduler; pricing question retained. |
| 11. Graph definition | Removed; DSL/IR and module navigation; roadmap owns CEL uncertainty. |
| 12. Scheduler | One scheduler view retained; three mechanics questions preserved. |
| 13. API | Removed; API specification; dry-run and health-serving questions retained. |
| 14. Data model and persistence | Removed; data-model specification; transition-ownership question retained. |
| 15. Worker | Single worker/runtime view updated; pipeline narration removed. |
| 16. Session runner | Protected-authority view and two questions retained; component taxonomy removed. |
| 17. Thin clients and the console | Removed; API/ADR-0015; skill-delivery question retained. |
| 18. How a change lands | Removed; AGENTS.md remains the owner. |
| 19. Common changes | Recipes removed; contract-owner links retained. |
| 20. Zones and contracts | Module/contract navigation retained without a second ownership taxonomy. |
| A. Diagram key | Appendix removed; surviving views explain their scope locally. |
| B. Glossary | Essential missing terms merged into the canonical glossary. |
| C. Namespace policy enforcement | Repeated table removed; policy-owner links and secret-prefix question retained. |

The old reference’s preamble is replaced; §1 context, §2 containers, §5 attempt sequence and §8 boundaries are removed; §3 scheduler and §4 worker are simplified; §6 deployment, §7 stack and §9 glossary survive under semantic headings. **The old sequence incorrectly makes publication universal.** The [scoped publishing walkthrough](../../docs/technical-introduction.md#4-one-run-through-the-core) and [normative recovery contract](../specs/scheduler.md#3-submission-execution-uncertainty-and-recovery) supply their respective explanations. No recovery sequence survives: old pictures duplicate or infer behavior owned by the specification. ([Baseline reference](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/detailed.md))

## Four diagrams preserve distinct architectural views

The retained reference has eight sections. Four contain diagrams; the others provide lookup tables and links to controlling sources.

| Heading | Retained content |
|---|---|
| [Components inside the scheduler](../architecture/detailed.md#components-inside-the-scheduler) | Passes, metadata, retention and unlocked external observations. |
| [Components inside a worker](../architecture/detailed.md#components-inside-a-worker) | Sealing, preparation, submission, reporting, uploads and conditional publication; supervision separated from orchestration. |
| [Protected execution boundary](../architecture/detailed.md#protected-execution-boundary) | Supervisor, candidate domain, private journal/checkpoint and separate publisher. |
| [Deployment shapes](../architecture/detailed.md#deployment-shapes) | Standalone, compose/pilot and remote workers; one MVP scheduler. |
| [Module and contract map](../architecture/detailed.md#module-and-contract-map) | Planned areas linked to their contracts; shared-component locations remain open. |
| [Stack per component](../architecture/detailed.md#stack-per-component) | Dependency/rationale/alternatives inventory with ADR-backed corrections. |
| [Glossary](../architecture/detailed.md#glossary) | Canonical terms with source links. |
| [Open implementation questions](../architecture/detailed.md#open-implementation-questions) | Thirteen questions with established constraints and controlling sources. |

The package-dependency diagram was considered but omitted: the compact module/contract table preserves its useful navigation without another architectural picture or implied API/scheduler invocation decision. Under a strict diagram budget, the protected-authority view contributes more information beyond that table. Compiler/API decomposition, state machines, races, cost flow and repeated graph examples add no missing contract. Navigation covers validation, execution, API parity, persistence, reporting, lifecycle, conformance and cross-cutting policy/approval/cost; it does not invent packages or duplicate future test catalogs. ([Module source](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#5-code-map), [contract source](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#20-zones-and-contracts), [authority source](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#16-session-runner))

## Thirteen questions retain their decided constraints

“Open” below means unresolved in repository sources; live issue status was not fetched. Each linked source identifies the original question and conflicting owners.

| Unresolved question | Constraints already established |
|---|---|
| [Package placement](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L482): runner, executor, shared client, persistence, shared validator? | Main layout exists; these locations remain unspecified. |
| [Run outcome](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1005): succeeded versus failed aggregation? | Failed nodes remain nonterminal/resumable; completion wording supplies no selecting predicate. |
| [Pricing](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1059): who writes price_table_id/USD and distributes the snapshot? | ADR-0010 decides snapshotting and write-time derivation, not process ownership. |
| [Timeout pass](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1153): per-row discovery/per-run mutation locking? | ADR-0022 requires run-lock deadline guards; broader pass ordering remains open. |
| [Budgets](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1267): daily/run/subgraph checks relative to scope checks? | ADR-0020 fixes run-first, global→namespace→graph→runtime→fan-out→target ordering; acquire nothing before all checks pass. |
| [API/scheduler invocation](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1271): mechanism and tick timing? | Three shared entry points and canonical start implementation are decided. |
| [Dry-run](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1350): execution placement and complete payload contract? | Fake runtime/harness required; author-side compilation is separate and settled. |
| [Health serving](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1352): scheduler/worker endpoint servers? | NFR-7.3 requires component health/readiness/metrics; API routes alone do not specify serving. |
| [Transition ownership](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1379): reconcile “only passes” with reporting writes? | API-mediated submitted/finishing reports are decided; delegation and ownership wording need reconciliation. |
| [Command inputs](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1555): who materializes .kora/inputs under runner execution? | Worker wording conflicts with workspace access; ADR-0016 settles authority, not this handoff. |
| [Digest errors](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1559): skill/MCP mismatch classification? | Pinning and verification required; no mapping to the closed error vocabulary is specified. |
| [Skill delivery](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1577): deploy-time content source and execution-time transport? | ADR-0018 fixes identity/history; delivery remains deferred in #45/#48. |
| [Secret prefixes](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md#L1727): namespace policy or provider configuration owns them? | Validation/resolution enforcement required; configuration ownership remains inconsistent. |

Existing decisions support separate corrections: ruamel.yaml, Docker runtime, observability and testing are decided; Docker uses aiodocker; credential proxy is post-MVP. The Git implementation mechanism remains proposed. One-scheduler MVP placement, control-plane-only event exporting and protected supervisor placement are established constraints. Timeout does not prove termination; result authority includes deadline guards; pricing ownership remains open; recovery-only eligibility is already resolved. ([ADR-0013](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/decisions/0013-implementation-stack.md), [ADR-0004](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/decisions/0004-harness-adapters-over-vendor-sdks.md), [ADR-0016](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/decisions/0016-trusted-execution-and-publication.md), [ADR-0022](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/docs/decisions/0022-inherited-execution-deadlines.md))

## Live navigation and historical evidence have different destinations

The affected live references are AGENTS.md, README, both documentation indexes, introduction links, functional requirement decision 18, API console references and ADR application lists. Their destinations depend on subject: introduction for context/boundaries, specifications for runtime/recovery, named reference anchors for stack/components/modules. Numbered-section prose is also affected. The AGENTS dependency registry destination remains stable; issue/PR template structure is unaffected. ([Baseline navigation](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/README.md))

Historical citations retain findings, identifiers, section numbers and dates. Introduction research uses its `bd817fbf8d42643d639e3beccb67646a12183c9c` baseline; architecture critique uses `d049b2b4948ff58c4651db009ab17cb02be7893d`; older review/design-decision registers use the explicitly identified pre-consolidation baseline above. Provenance notes replace the obsolete “same section numbers” alias. The inherited-deadline review and Archon comparison retain pinned kora sources; Archon’s own filename is unrelated. The earlier recommendation to preserve both reading depths remains historical evidence of its original scope. ([Earlier research](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/research/technical-introduction-2026-10-17.md), [historical review](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/reviews/review-2026-10-15.md))

Validation scope includes deleted paths, active links/anchors, numeric references, thirteen questions, stack-row preservation and pinned historical targets. Offline Lychee cannot verify remote snapshots. Existing CI covers Markdownlint, Lychee, Mermaid rendering, IR examples and GitHub templates; research files need explicit linting because the normal configuration excludes them. Diagram rendering also needs visual inspection. This audit records no completed checks or owner review. ([CI](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/.github/workflows/ci.yml))

## Integration with concurrent specification updates

The final change integrates master at `63d658aa3e1a5692f8a5a742ce7b041463b39fbe`, including PRs #73, #74 and #75. Their additions to the retired book/reference restate contracts retained in [retry invalidation](../specs/data-model.md#6-clear-downstream-fr-68a), [branch ownership and selection](../specs/ir.md#45-branch-ownership-and-selection) and [durable readiness discovery](../specs/scheduler.md#8-restart-and-multi-scheduler-safety). The reference links those owners and keeps the refined clear-downstream definition. The three dated review records and all specification/decision amendments are preserved. No additional open question was introduced by those book changes; the thirteen-row register remains applicable. The original source citations above retain the research baseline rather than silently moving to the integration revision.
