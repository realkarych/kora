# Complete tool-policy coverage requirements

Checked on 2026-11-02 against source `60e88c54edd1af5503791040080c6ce47aa8e5e9`. [Issue #636](https://github.com/realkarych/kora/issues/636) implements the [owner-accepted proposal](https://github.com/realkarych/kora/issues/549#issuecomment-6580856633), SHA-256 `7b2dde4f42453a12e6ab237954ac6cd42f8a170cfe7ce3e05c2f66a1450986bd`; the [approval record](https://github.com/realkarych/kora/issues/549#issuecomment-6581556814) and [implementation plan](https://github.com/realkarych/kora/issues/636#issuecomment-6581576928) identify the scope. The normative decision is [validation-facts section 9](../specs/validation-facts.md#9-complete-policy-coverage-requirements), governed by ADR-0009 and ADR-0026.

## Source findings and decision

The original five-literal `CallKind` type included four primary operation kinds and the subagent context. It did not assign ordinary local read/search or delegation-control calls to an operation category. The accepted grammar permits arbitrary tool names, and the existing `allow_deny` contract explicitly defines by-name behavior for a `Read` argument without an extraction rule. Consequently, all five original flags did not prove interception for every ordinary call.

The source also establishes requirements beyond the kinds matched by individual patterns:

| Source boundary | Established behavior | Required treatment |
| --- | --- | --- |
| `allow_deny.by_name` and specialized shell/edit evaluation | A nonempty allow array denies calls without a matching allow | The accepted conservative rule requires every primary operation kind, even for a wildcard allow or an argument that cannot match |
| `allow_deny.shell_condition` | A pattern governing file edits or protected paths activates shell guards | File-edit policy requirements include shell coverage; protected-path obligations retain their independent owner |
| `allow_deny.patterns` | Deny aliases expand through Edit/Write matches; allow aliases expand only for exact Edit/Write; MCP server shorthand adds its tool-family match | Classification must preserve the actual matcher and cannot approximate all patterns by literal names |
| Harness capability and first-slice contracts | Built-in subagent calls remain relevant when authored subagents are absent; Codex hosted search is disabled but lacks interception | Subagent coverage is additional for every nonempty layer; disablement does not grant hosted coverage |

The owner selected one additional residual primary kind, `other_tool`, rather than a second per-tool interception representation. Its meaning includes every ordinary call outside shell, file edit, MCP and hosted tools; it does not classify an unknown name automatically. The ordinary-call universe includes calls that never reach a policy hook. Harness-internal actions and the exact existing output-channel exception retain their separate semantics.

This produces an exhaustive primary-kind upper bound while preserving unavailable native facts as unavailable. An unfamiliar authored pattern can require all primary kinds when the exhaustive native contract is complete; incomplete native classification or coverage cannot be repaired by demanding more flags. Current five-kind declarations do not silently become complete six-kind declarations.

The compatibility cost is explicit: every nonempty Codex allow list requires its specified uncovered hosted-tool kind and is refused. Deny-only policies can be admitted when all required kinds and subagent coverage are established. Whole-class residual coverage can also refuse a policy naming an intercepted tool if another tool of that class bypasses interception. Greater precision requires a separately accepted rule and evidence; it is not inferred from configured disablement.

## Implementation boundary

The production change only adds `other_tool` to the existing `CallKind` literal. `HarnessCapabilities.policy_coverage` keeps its existing set shape. No producer, matcher, provider declaration, accessor, loader, registry, API operation, persisted representation, diagnostic emitter or resource owner changes. The existing fake adapter explicitly declares shell, file edit and MCP coverage; its declaration remains unchanged and supplies no real-harness evidence.

The source/reference comparison and strict checks establish vocabulary consistency, not successful interception. No test is added merely to duplicate a Literal declaration. Existing harness models, policy decisions and shared conformance checks protect unchanged integration. Full final-head CI and actual independent review are recorded in the implementing PR; this record does not claim their results before execution.

## Verification and remaining activation

[Actual independent correctness and security design reviews](https://github.com/realkarych/kora/issues/549#issuecomment-6580885391) inspected the exact accepted proposal. Correctness required the explicit inclusion of unhooked calls; security traced allow-list exclusion, aliases, edit-shell guards, subagent scope and native completeness. Both concluded that the residual-kind decision is sound with the stated conservative compatibility limit. Design conclusions do not establish implemented consumer or real-interface behavior.

The affected threats are TM-03 policy interception/bypass and TM-12 trusted native completeness. The change supplies no new containment, credential authority or native enforcement guarantee. Observability activation remains with executable consumers because this type extension adds no emitting path. No performance, browser, numerical method or mutation campaign is added.

The latest inspected successful master mutation audit, [run 41297342266](https://github.com/realkarych/kora/actions/runs/41297342266), used source `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`. Its summary and report identify a budget-partial run, restored continuation, 113 executed entries and zero reused entries. Only pricing and CEL-path scopes were declared, so it has no applicable tool-policy classification finding. The later [run 41433056472](https://github.com/realkarych/kora/actions/runs/41433056472) failed and is not passing audit evidence.

Work item 15 must establish complete native classification and real-interface interception for each supported harness version/configuration before claiming the residual class. Work items 14 and 15 retain actual launch, lifecycle, normalization and conformance ownership, including #196/#197/#199. Work items 3/11 and #549 own configured selection, detached facts, the executable requirements classifier and whole validation; #593 owns new-start integration. Existing error precedence, authorization, saved execution contracts and permanent replay remain unchanged. Adding the shared type or publishing this decision activates none of those consumers.
