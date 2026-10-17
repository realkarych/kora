# Branch target gating review

Date: 2026-10-17. Issue: [#69](https://github.com/realkarych/kora/issues/69). Disposition: confirmed; resolve before branch validation/readiness implementation. The project owner accepted one owner per direct target, explicit control edges, durable selection and the MVP invariant restriction described below. The decision is recorded in [ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md); the project owner accepted the final wording on 2026-10-17.

## Evidence

The old [IR edge definition](../specs/ir.md#3-nodes) included only inputs and `depends_on`; branch target validation required existence but not the branch control dependency. The DSL's `branch(... then=...)` example did not require `>>`. A target could therefore appear ready before its delayed branch evaluated. The canonical SQL optimization IR already contains `accept.depends_on: [decide]`, demonstrating the intended lowering shape.

An in-memory JSON Schema/topology probe accepted a delayed route and a data-independent target without that control edge: the target was a root. Adding the control edge removed it from the roots, including with `one_success`. A route that depended on its own target was also an ordinary DAG until the omitted routing dependency was included. These probes cover document shape/topology only; kora has no semantic validator or scheduler implementation. `always` is not a valid trigger rule and is not used as evidence. Existing all-upstreams-terminal semantics already prevent a pending owner race when its explicit edge is present.

Independent counter-review established why lowering alone is insufficient: route R excludes B; B also depends on A. Retrying A clears B but leaves R successful. Unless readiness checks the retained selection, an ordinary dependency check can now execute B. Writing `skipped` only when R first evaluates loses the exclusion.

## Accepted correction

The DSL inserts a direct ordinary control edge for every ordinary target. Semantic validation requires the edge, rejects multiple distinct owners and checks ordinary DAG cycles. Multiple cases of one owner may name the same target. `$fail_attempt` remains an action.

The current concrete branch instance stores a compact selected-target set atomically with success. Every target readiness check consults it alongside ordinary dependencies. Unresolved owners block, skipped owners/exclusion skip, and failed/upstream-failed owners yield `upstream_failed`. Clearing the owner revokes its selection; clearing only the target and expiring input payloads do not. Untargeted joins retain their trigger rules; no transitive blanket skip is introduced. The owner control edge counts normally in trigger aggregation.

This reuses explicit IR dependencies and the existing node control record. It adds no hidden edge category, physical attempt or payload retention hold. Input independence never bypasses the gate.

## Invariant reuse and current routing

Postimplementation cross-review found a second interaction with subgraph iteration: `attempt_invariant` preserves a successful target without rematerialising it. If iteration 1 selects T and iteration 2 excludes it, T never returns to pending readiness, so its old success can bypass the new decision. An invariant branch likewise has no new concrete owner in the next iteration. A pending-only routing check cannot close either case by itself.

On 2026-10-17 the project owner selected an MVP validation restriction: reject `attempt_invariant: true` on branches and on all their ordinary direct case/else targets, using IR010. Each new body iteration therefore materialises those nodes and applies its own route. Ordinary non-target invariant nodes remain supported. Designing a separate activation-local gate around cached results was considered and not selected for MVP. This restriction changes valid graph definitions rather than silently changing cached execution at runtime.

## Verification boundary

[Data-model section 5.1](../specs/data-model.md#51-current-branch-selection) names future conformance cases for delayed and nested routes, exclusions, permissive triggers, target-only retry, changed owner selection, payload expiry, invalid ownership/cycles, invariant-routing rejection, current-route behavior across iterations and untargeted reconvergence. Runtime cases must use both metadata databases; they have not run in this design-only repository.

Current CI checks Markdown, links, Mermaid, both canonical IR examples and GitHub templates. JSON Schema acceptance alone does not prove the cross-node semantic rules.
