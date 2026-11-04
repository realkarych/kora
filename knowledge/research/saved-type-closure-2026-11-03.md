# Saved execution-contract type closure

Status: exact representation accepted by the project owner on 2026-11-03 under [#662](https://github.com/realkarych/kora/issues/662), a child of [#593](https://github.com/realkarych/kora/issues/593). The [plan and independent design results](https://github.com/realkarych/kora/issues/662#issuecomment-6614884812) and [owner decision](https://github.com/realkarych/kora/issues/662#issuecomment-6614907944) separate format selection from implementation. [Data-model section 11.1](../specs/data-model.md#saved-type-closure-section) is normative. This change implements no producer, lossless edge extractor, complete reader or execution path.

## Decision and alternatives

`execution_contract.type_contracts` is a canonical type-name-ordered array with one exact eight-member record per authoritative IR declaration, including unused declarations. Records save name/revision, parent/behavior identities, the complete built-in descriptor where applicable, logical source root, exact canonical schema and complete resolved reference edges. Source roots remain distinct; general reference cycles are edges, never expanded. The selected record order applies to this derived section, preserving authored schema order and the already accepted parameter binding's independent order rule.

A shared deduplicated schema table would add document indirection and ownership rules. Saving only revision names and looking up installed definitions later would not supply ADR-0018's required by-value closure. The chosen shape directly extends the seven-member records already consumed by StructuredOutput preparation with saved general-reference metadata, while leaving that input ABI unchanged through explicit adaptation.

Original built-in schema hashes keep their published file-byte meaning. The canonical stored schema object retains the logical value; its reserialization does not replace a published file hash. Built-in metadata comes from trusted exact revision definitions, and custom schemas/parents/behavior from pinned canonical IR. A stored descriptor or behavior identity grants no arbitrary code-loading authority. Existing record, semantics, physical encoding and domain digest identities remain unchanged, and no new standalone digest is introduced.

## Implementation follow-up

The real type-section producer, lossless reference extractor and checking reader are implemented under [#664](https://github.com/realkarych/kora/issues/664), with their [source, capacity and verification record](graph-type-construction-2026-11-04.md). The paragraphs below preserve the original design obligations; the preparation adapter is implemented under [#667](graph-schema-preparation-2026-11-04.md), while the complete execution contract remains pending. The implementation plan refined the initial 500-line estimate to at most 850 production additions/deletions, retaining the 1,000-line hard maximum.

## Original implementation gaps and owners

The general IR `SavedReferences.resolves` facility returns a boolean. General schema diagnostics redact named positions and may truncate deep paths. Their outputs cannot be copied into immutable edge identities. A future bounded lossless source/target producer must preserve the accepted general resolver rules, distinguish original document roots and schema positions from annotations, and preserve exact string keys versus integer array indices. Its actual code and admission method require a detailed implementation plan and independent review.

The executable StructuredOutput preparer serves a narrower purpose. Its adapter must select the relevant output closure under preparation's own rules, including inline-schema dependencies; remove saved general-reference metadata; exclude extra types reachable only through inert `$recursiveRef`; and retain its own target derivation and refusal behavior. General reference cycles can be saved even when executable-call cycles refuse preparation. Parent cycles remain invalid. No saved section claims that its contents fit the independent 64-document, 4,096-edge or other complete preparation limits.

Checking expected derived values under an explicitly supported immutable semantics is comparison only. The reader validates its inputs/dependencies first and returns saved interpretation unchanged. A caller-supplied estimate, projection or flag cannot prove itself. Canonical IR normalization, trusted namespace/object/version selection and original publication remain independent obligations; a self-consistent dictionary with recomputed digests is not an authenticated published version. Historical resolution provenance is established by real owners when written, not manufactured by reading current aliases/defaults. Current native compatibility, provider completeness, permissions and availability remain separate admission checks.

The later implementation targets at most 500 added plus deleted production lines, with the repository's 1,000-line hard maximum and separate slices if needed. No package-layer widening, dependency, provider ABI or migration is selected. Before code, the plan must account for all admitted schema, index, path, sort and comparison state. Edge count alone does not bound paths: R edges at depth D may retain O(R × D) segments plus repeated-name bytes. Per-output component limits also do not bound aggregate repeated preparation. The full composition owns those totals and transient lifetimes outside database locks; raw acquisition and a finite backend-compatible public budget remain separate activation work. No new numerical default or memory/time guarantee is approved here.

## Pending executable acceptance

These were future obligations in the contract-only change. Under #664 the inventory, reference, checking, capacity and persistence tests are executable; preparation adaptation is exercised under #667 through `test_graph_schema_preparation_selects_exact_closure` and its refusal/admission regressions:

| Owner and named test | Required behavior |
|---|---|
| Type section producer, #593: `test_graph_type_contract_inventory` | Real built-in/custom schemas and descriptors; exact full inventory and parent/source agreement; original root and canonical scalar identity preserved. |
| Lossless reference producer, #593: `test_graph_type_contract_references_lossless` | Sibling and deep paths, string key `"0"` versus array index `0`, recognized schema positions versus annotation data, exact local/saved targets, complete keyword inventory and undeclared-target refusal. |
| Type section reader, #593: `test_graph_type_contract_rejects_inconsistent_saved_values` | Missing/extra/changed records or targets refuse even with recomputed outer digests; unsupported revisions/default changes never repair history; cycles follow their distinct owners. |
| Preparation adapter, #593: `test_graph_type_contract_preparation_boundary` | Correct selected-output closure, inline dependencies and unchanged seven-member ABI; inert metadata cannot authorize executable assertions or bypass existing preparation refusals. |
| Capacity owner, #593: `test_graph_type_contract_capacity` | Aggregate admission before oversized schema/path/index/sort state; bounded cycles without expansion; capacity exhaustion never yields a valid partial result. |
| Physical integration, #593: `test_graph_type_contract_round_trip` | Real SQLite/PostgreSQL persistence and scoped selection, checking after read units/checkouts release, actual connection closure, preserved canonical identities. |

Complete effective-output/checker/validation records and their digests, retained actual checked CEL renderings/costs/frozen path sets, resolved retry/resource facts and closed whole-contract composition remain subsequent #593 work. Complete provider facts and approval contexts retain #549/#552 ownership. No partial section or current registry/default authorizes deployment or execution. Authorized history, accepted/skipped replay and recovery/closure preserve their existing order.

## Verification and observability limits

The selected format received separate read-only correctness/security/performance design review against `a31fcd1500aa0a7cee50b96e342049326a454582`. Final proposal SHA-256: `66999abde646dbb667003a3e298d948cb1abc6a27f4db453dda7ebe598e1dc60`. Actual reviewers, inspected inputs, bounded delta reuse and conclusions are recorded in the issue. Design approval supplies no executable readiness evidence.

This contract-only change has zero production additions/deletions and requires actual-diff correctness/security/performance review, changed-document checks and one complete successful final-head Linux/macOS CI. No test, platform, deadline, numerical acceptance method or campaign rule is weakened. No unchanged CI rerun is authorized. Applicable retained master-audit findings concern pricing/CEL, not this unimplemented section; no new mutation campaign or merge hold is created.

No new actor, credential, runtime authority, diagnostic sink or event is introduced. Existing TM-01, TM-04/TM-10, TM-06/TM-07 and TM-08/TM-11/TM-12 cover the provenance, untrusted content and immutable-interpretation boundaries. Saved raw paths and schemas are retained content, not filesystem or diagnostic authority. The implementing issue must supply fixed content-free errors and applicable structured activation diagnostics. Current publication/resource ownership and complete consumer checks remain mandatory.
