# Retry dependency invalidation review

Date: 2026-10-17. Issue: [#68](https://github.com/realkarych/kora/issues/68). Disposition: confirmed design contradiction; resolve before manual retry and clear-downstream implementation. The project owner accepted input-name independence and the implementation plan on this date. Normative decisions are in [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md); the project owner accepted the final wording on 2026-10-17.

## Evidence and counterexample

The [IR](../specs/ir.md#3-nodes) defines data edges through declared inputs separately from `depends_on`. Before this correction, [data-model section 6](../specs/data-model.md#6-clear-downstream-fr-68a) and ADR-0007 described retry traversal through `consumed_versions` and control edges only. Bindings are recorded at readiness; an unsuccessful producer may prevent its consumer from reaching that point.

Let A exhaust automatic retries before producing output. B has a data-only input from A and becomes `upstream_failed` without an attempt or consumed version. After recovery settles, an otherwise eligible manual retry of A can succeed, but the old traversal cannot reach B. B remains terminal despite FR-1.6/FR-6.8a promising downstream reconsideration. This is a specification counterexample, not an observed runtime failure: the repository has no scheduler implementation.

Independent reviews checked whether existing rules already close the gap. Immutable physical-attempt bindings (ADR-0018) cannot cover B when no attempt exists. Adding redundant control edges in every author graph is not required by the DSL. Automatic retry before final failure is a different case and is not the counterexample. Existing expiry, input-availability, capacity and execution-recovery guards remain necessary but do not supply the missing dependency.

## Accepted correction

- Traverse current concrete dependencies from declared data/control edges, body input forwarding, exports and collectors, supplemented by current bindings. Retain active reuse of earlier invariant artifacts without reviving obsolete iterations.
- Interpret `independent_of` as input names. Preserve only a successful outcome, per affected input edge; other affected inputs or control edges override the exemption. Unexecuted/non-successful consumers are reconsidered.
- Distinguish a full node/wrapper clear from child-only export invalidation. The latter rearms collection with the same body and unaffected siblings; full wrapper/input/control clear resets the current body. Full clear takes precedence when both reach a wrapper.
- Preserve immutable history, usage, target identity, inherited clocks and old-execution recovery barriers. Readiness still checks routing and actual trigger/input rules.

These rules amend the existing clear transaction; they do not add an execution generation, dependency table or API operation.

## Verification boundary

The named cases in [data-model section 6](../specs/data-model.md#6-clear-downstream-fr-68a) are future integration gates for both PostgreSQL and SQLite. They cover unconsumed dependencies, successful versus resultless independence, converging exempt/non-exempt paths, child recollection, changed wrapper inputs/control dependencies and active invariant reuse. Existing late-result/deadline conformance remains required. No runtime test is claimed as executed.

Current executable verification is the repository's Markdown, local-link, Mermaid, IR-example and GitHub-template CI. It verifies document integrity, not scheduler behavior.
