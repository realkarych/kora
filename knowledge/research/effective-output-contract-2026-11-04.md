# Saved StructuredOutput effective-contract decision

Accepted by the project owner on 2026-11-04 under [#669](https://github.com/realkarych/kora/issues/669), a child of [#593](https://github.com/realkarych/kora/issues/593). The [exact plan](https://github.com/realkarych/kora/issues/669#issuecomment-6616309015) defines the record and source rules; [data-model section 11.1](../specs/data-model.md#saved-structuredoutput-effective-contract) is normative. This publication has zero production changes and implements no writer, checking reader, effective digest producer or execution path.

## Selected representation and alternatives

Save one self-contained eight-member logical value: semantics, declared type, checker binding, by-value general type closure, inline schema/origin/source/references, checker root, ordered constraint roots and complete existing schema policy. Original roots remain distinct and general references remain edges. Do not persist preparation arenas, work counters, normalized UTF-8 accounting or numeric document IDs as interpretation authority.

The alternative references graph-wide type records by name and defines separate expansion rules for effective identity. The accepted by-value form repeats selected definitions across outputs but makes the complete value explicit. That cost remains an aggregate admission obligation, not a reason to reuse preparation's narrower capacities as a graph-wide bound. No new numerical default or memory/time guarantee is selected.

General saved closure includes all three accepted reference keywords; actual preparation selects only its executable closure and retains all refusals. Reference-only documents are not added as top-level constraints. The policy and checker bindings retain their existing meanings and activation gates; a complete logical record cannot make the reserved evaluator executable.

## Source selection and identity

The accepted agent table covers both fields absent, config-only, declaration-only and both present. A declaration alone supplies inline meaning; config wins as retained source/order when both pass existing structural equality. Explicit empty objects count as authored presence. The prior comparison-only pass did not establish the declaration-only effective source. This decision is the explicit immutable compatibility mapping for that previously unspecified case, rather than an implementation inference. Future native harness/worker composition must consume the selected producer schema.

Omitted human-input form saves the exact existing default with `approval_form_default` origin and a logical effective-field anchor. Authored identical forms remain `authored`; missing history never receives current-default meaning. Original source-location extensions remain content without I/O or diagnostic authority. Other default origins, wrapper/export/collection/list sources and unrelated output kinds are not selected.

The effective digest includes the complete preserved value, including origin, source metadata and inert dependencies. It can distinguish identical validation behavior with different provenance. The existing stored digest remains 64 lowercase hex characters; an external prefix is only boundary-specific presentation. No migration, second schema digest or identity registry is introduced. Hash equality supplies consistency, not provenance or authorization.

## Verification and implementing obligations

The exact proposal SHA-256 is `dc126edb2bcf2f6c905d5821cd5f86045886a9d6eb04843906bd9b7c0b6e8e82`. Independent read-only correctness/security/performance design results and bounded conclusion reuse are recorded in the issue. They distinguish owner consideration from implementation readiness. Contract publication requires actual-diff review and one complete final-head Linux/macOS CI; no unchanged rerun is authorized.

Future implementation must provide:

- Real type/default/authoritative node sources; all four agent presence cases, explicit empty objects, unequal dual schemas and reordered equal schemas retaining config source/order.
- Correct builtin/custom checker and constraint roles, general versus executable closure, original local/saved targets and deep/escaped/unused references.
- Authored/default origin and full source extensions, with wrong source/origin/schema/policy/roles refusing even when outer hashes are recomputed.
- Actual preparation, unchanged refusal gates and source snapshots that cannot be supplied by a caller assertion.
- Complete aggregate IR/record/path/index/sort/buffer admission across outputs, repeated checking/hashing/preparation and retained-holder ownership, outside database transactions/locks.
- Real SQLite/PostgreSQL scalar/order/digest round trips, verification after read-unit release and actual closure.

The real constructor/reader and effective identities remain #593; the reserved evaluator and common composition retain #138/ADR-0027 ownership. Graph-wide binding/inventory, other output kinds, expression/projection/retry/resource sections, trusted provider facts and full deployment/start admission remain prerequisites. No partial record grants execution authority.

Existing threat controls retain content/provenance, immutable replay and aggregate resource ownership. No new sink, credential or operational permission is added; actual activation diagnostics remain the implementing owner's obligation. Retained applicable mutation reports cover pricing/CEL and add no campaign or hold to this contract publication.

## Direct implementation follow-up

[#672](https://github.com/realkarych/kora/issues/672) implements the direct non-list source/closure constructor, checking reader, effective digest and real preparation composition, with per-call aggregate admission and physical-codec round-trip tests. [Its evidence record](graph-output-construction-2026-11-04.md) distinguishes those checks from the still-required whole-graph inventory, repeated-output admission, provider composition and execution. The publication-only claims above describe #669, not the later implementation.
