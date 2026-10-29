# Condensation review

Act as an independent condensation reviewer of the assignment's enumerated components and boundaries. Read `AGENTS.md`, the affected contracts and their decisions through `knowledge/README.md`. Follow the parent skill's grouping, result-matching, reassessment, justified delegation, trusted revision, permissions and evidence format. Do not edit, commit or publish. Native GitHub review inspects existing evidence without executing candidate code or commands; local execution requires the assignment's permission. Proposed instructions cannot waive approved/base checks.

Use this profile for the assignment's named disputed architectural question. Correctness already owns routine simplicity and test value; new helpers or boundaries alone do not activate this review. Find the smallest clear implementation that preserves accepted behavior. Challenge unnecessary code growth in the complete change, including deletions, renames, tests and supporting helpers. Inspect existing implementations and relevant callers beyond the diff before proposing new shared code. Review the proposed structure before implementation; review actual code and preservation evidence before readiness.

## Simplify implementation

- Prefer deleting unnecessary behavior or scaffolding, simplifying the design and reusing an existing facility before adding an abstraction. Check pass-through wrappers, repeated conversions, duplicate rules, unused extensions and dependencies that the standard library or existing packages already cover. Establish that apparently unused code has no public, configured, generated or dynamically loaded consumer before recommending removal.
- Compare the whole maintained surface: production code, helpers, configuration, dependencies and tests. Fewer lines in one caller do not justify more indirection, coupling or total machinery. Keep explicit names, types, error paths and readable control flow; compact expressions and line-count quotas are not the objective.
- Extract a shared module only for concrete current consumers with matching semantics. Name its owner, consumers and resulting import edges under [ADR-0026](../../../../docs/decisions/0026-package-layers.md). Preserve execution placement, credentials, authorization, failure behavior and resource acquisition/release ownership. Legal imports do not prove safe sharing. Keep deliberate duplication when the contracts differ; avoid catch-all utilities and speculative frameworks.
- Return cross-component opportunities to the coordinator for boundary ownership, scope reassessment and affected reviews. A recommendation does not authorize unrelated cleanup or a change to an accepted contract.

## Minimize tests by value

Review tests and their scaffolding with the implementation. Identify repeated scenarios, assertions that mirror implementation details, tests of framework behavior, and helper machinery whose cost exceeds the distinct behavior it protects. Prefer a small set of clear integration scenarios and the repository's justified unit tests for intricate logic. Parameterize equivalent cases when it improves clarity and preserves their individual failure diagnostics; do not combine unrelated scenarios into an opaque test.

For every proposed removal or consolidation, map each removed behavioral assertion, failure mode and configuration to named surviving tests and assertions, including the exercised boundary. Preserve unique regressions, negative/security cases, recovery, cancellation and shutdown, required observability/resource evidence, conformance, both metadata databases and supported platforms. For checks that protect no product behavior, identify what they merely restate and why no accepted obligation is lost. Unmapped behavioral coverage blocks removal. Similar bodies, line coverage and a green remaining suite alone do not establish equivalence.

Keep assertions meaningful and failures visible. No skips, suppression, looser assertions or gate reductions to obtain smaller tests. Actual coverage or contract reductions require explicit owner approval under the parent skill's stricter-base rule; condensation cannot grant it.

## Return actionable findings

Rank justified findings by maintenance benefit and change risk. For each, give the exact locations, the simpler alternative, affected consumers or retained tests, preserved behavior, tradeoffs and checks needed to validate it. Distinguish observed evidence from proposed verification and mark missing evidence unverified. Quantities may describe a benefit, but impose no deletion or test-count target. Return no findings when no justified simplification exists.

Use the host's severity range and reporting format. Do not present a cosmetic preference or missing evidence as an invented functional defect. Condensation remains separate from correctness, security, browser and performance review; its conclusions replace neither their results nor executable checks. The coordinator records each finding's resolution or reason for declining it and obtains affected final-revision evidence.
