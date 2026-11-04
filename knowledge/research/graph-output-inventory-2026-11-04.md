# Saved graph-wide output binding inventory

## Decision and scope

The project owner accepted the direct by-value array under [#684](https://github.com/realkarych/kora/issues/684) on 2026-11-04. The [normative section](../specs/data-model.md#saved-output-binding-inventory) defines required `execution_contract.outputs` entries containing exact `path`, complete `contract` and existing `contract_digest`, ordered by canonical path. This publishes an outer association only; no aggregate constructor, checking reader or execution consumer is implemented by this change.

[ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) requires immutable by-value deployment meaning. Direct StructuredOutput contracts under #669/#672 and exact native contracts under #675/#679 intentionally leave whole-output inventory and binding separate. Equal effective values can belong to different declarations; a leaf digest cannot locate or authenticate its source. The outer section participates in existing execution-contract and deployment identities without changing leaf digest scope, engine semantics or physical encoding.

## Completeness and interfaces

The three path shapes cover root-node outputs, every stored body's node outputs and body exports. The graph owns a single stored-body registry; nested wrappers reference it rather than creating another declaration scope. Include unused definitions, preserve exact canonical string segments and never expand invocation instances. Empty maps add no entries, and missing implicit roles remain existing IR failures.

Every entry must carry a complete supported effective interpretation. No null, placeholder, skipped or unsupported declaration can produce partial success. Body exports retain their declared interface rather than inheriting a narrower internal producer's type. ReviewBundle, native inline/custom forms, lists, exports, wrappers and collections still need their own supported composition and aggregate gates. The first-slice restriction on stored bodies remains independent of this general representation.

Readers require complete unique ordered inventory, exact members and digest, supported meaning, expected declaration/source agreement and trusted namespace/object/version selection. They return saved values without current aliases or default repair. Missing older interpretation refuses new effects while preserving historical reads, accepted/skipped replay and closure. Swapping unequal values between paths can invalidate source agreement despite valid leaf hashes; exchanging equal native values at already-correct paths changes no interpretation.

## Alternative and cost

A shared contract table with path references could reduce repeated definitions, but would add reference identity, exact closure and compatibility obligations. The selected direct array keeps each association self-contained and matches the existing leaf formats, at the cost of repeated by-value state.

Existing leaf builders parse/check complete sources per call, and readers can hold supplied and expected representations plus hash buffers. Reusing a leaf byte allowance for every output does not establish an aggregate bound. The full implementation must own inventory count, repeated whole-source/preparation work, paths and sorting keys, retained values, serialization/hash scratch, acquisition, frequency and concurrency outside metadata transactions and locks. Its plan must select explicit caller budgets and an aggregate algorithm before code. This decision chooses no public numerical default, memory/time guarantee or new campaign.

## Threat applicability and verification

Existing TM-04/TM-11 cover malicious declarations and contract substitution; TM-01 preserves scoped read authority; TM-10 owns aggregate work/state admission; TM-06/TM-07 cover future safe diagnostics; TM-12 separates accepted format from implemented controls and evidence. Existing threat-model entries already cover these boundaries. No actor, permission, runtime capability, retention or evidence-validity claim changes, so this contract-only refinement needs no threat-model text edit.

Independent correctness, security and performance design review passed for the outer format. Contract readiness requires actual-diff review, changed Markdown/link checks and one complete final-head Linux/macOS CI. No new unit test mirrors prose and no runtime/resource measurement is claimed by publication. Retained master-audit evidence inspected for planning had no applicable output-inventory finding; that absence supplies no mutation proof and adds no campaign or hold.

## Implementation ownership and activation

[#593](https://github.com/realkarych/kora/issues/593) owns the aggregate constructor/reader and complete execution-contract integration, using the actual direct constructors from #672 and #679 where supported. Its implementation issue must cover exact root/unused-body/export inventory; missing, extra, duplicate and reordered entries; mismatching source substitutions with recomputed leaf hashes; canonical path segments; refusal of partial unsupported graphs; real leaf/checker composition; exact and one-under aggregate admission before growth; bounded repeated work/state; both-database fidelity and actual read-unit release before checking; and content-free observability. Existing wider-composition and native/list aggregate tests remain prerequisites, not inferred passes.

The metadata caller retains trusted object/version selection and actual resource release. Provider facts, full validation, remaining saved sections, publication, native payload/projection consumers, retention and graph execution keep their existing owners. A complete output section alone activates none of them. No unchanged CI retry, native probe, deadline change or additional evidence framework is authorized by this contract slice.

The complete supported-direct constructor/checking reader is implemented under [#690](https://github.com/realkarych/kora/issues/690), with [cumulative admission and integration evidence](graph-output-inventory-construction-2026-11-04.md). It supplies no wider composition, full source-acquisition guarantee or execution activation.
