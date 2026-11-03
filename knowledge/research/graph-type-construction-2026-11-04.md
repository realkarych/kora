# Saved type-section construction and checking

Implemented under [#664](https://github.com/realkarych/kora/issues/664), after the owner-approved [saved type format](saved-type-closure-2026-11-03.md). [Data-model section 11.1](../specs/data-model.md#saved-type-closure-section) remains normative. This slice supplies the complete type section, not the complete execution contract or permission to publish or execute a graph.

## Sources and interpretation

`engine.graph_types` takes complete compact ASCII canonical IR object text. The existing strict iterative JSON reader and renderer establish canonical spelling before the real `GraphDocument` parser and declaration checks run. Custom schema roots, parents and behavior identities come from this private decoded document; current aliases and provider configuration supply nothing.

The four supported native definitions expose detached descriptors beside their existing trusted constants. `ir.schema_catalogue.builtin_schema_definition` reads each fixed packaged schema once and derives both its parsed root and published-file SHA-256 from those same bytes. The producer verifies that hash against the native descriptor. Real keyword validation, `SavedReferences`, lossless extraction and emission share those captured roots. The existing `builtin_schema` API and logical values remain unchanged. Caller mutation of returned descriptor data cannot change the trusted definition.

`engine.graph_type_references` walks schema positions iteratively with parent-linked paths. It retains exact string object names and integer array indices, including deep and escaped paths that diagnostic locations cannot represent. It uses the existing general resolver's keyword, position and fragment rules; annotation values do not become schemas. General reference cycles are recorded as edges without expansion, while declaration checks reject parent cycles. The three general reference keywords retain their existing static semantics and do not establish executable assertion support.

The producer emits every authoritative type declaration in the accepted eight-member order and canonical type-name order. Reference records sort by canonical source-path text. The checking reader admits an untrusted decoded section, constructs the expected section from the pinned canonical IR and actual sources, and compares exact text. Missing, extra, reordered or substituted values refuse; successful return preserves the saved text. This comparison supplies no provenance, current authorization, whole-contract identity, publication or provider compatibility claim. Test-owned persistence composition does not make a partial contract executable.

## Capacity and lifetime

The caller supplies a positive explicit budget B. Input IR text I is admitted before decoding; generated section text S is charged so that I + S is at most B. The original author-side materialized input and raw SQL acquisition remain outside this helper. Candidate-derived parse, root, lookup and traversal state derives from admitted I; generated retained path/sort/record state derives from S. Trusted built-ins add at most four fixed source definitions.

Path cardinality is admitted before constructing a temporary segment vector. Escaped scalar and complete path bytes are charged before retaining sort keys and records. The object-only codec renders arrays through a fixed one-member wrapper with six extra local syntax bytes; only the actual array is charged globally, preserving exact I + S acceptance. Output joins, the checking reader's expected value and other constant-factor temporaries coexist. B is not a process heap limit. Sorting and repeated resolution can be nonlinear; no latency, linear-time or RSS guarantee is claimed.

Existing schema validation keeps its regex time/space/interruption limitations and dependency-owned entry-bounded cache. Complete composition still owns frequency, concurrency, aggregate other sections and repeated output preparation. Exhaustion returns a fixed failure, never a valid partial section. No numerical default, dependency, provider ABI, migration or acceptance gate changes.

Construction precedes the database write transaction. Verification follows owner-scoped raw lookup and release of read units/checkouts. The paired physical codec preserves the section on SQLite and PostgreSQL. Actual database closure is tested. Resource reads use only the four trusted package resources; no input path, URL or arbitrary code identity is opened.

## Verification and pending activation

| Test | Distinct obligation |
|---|---|
| `test_graph_type_contract_inventory` | Complete unused/custom/built-in inventory, published descriptors and raw-file hashes, source detachment and canonical scalar identity |
| `test_graph_type_contract_references_lossless` | Deep/sibling/escaped paths, numeric key versus array index, local and saved roots, all three keywords, ignored annotations and cyclic edges |
| Saved-value, inventory and reference refusal tests | Missing/extra/duplicate/reordered/substituted records, scalar type or exact target changes fail content-free |
| Invalid source/declaration/canonical IR tests | Actual source validation, unsupported references/revisions, missing parents and parent cycles refuse |
| `test_graph_type_contract_capacity` | Exact and one-under combined admission for empty, escaped, huge integer and deep data, without process-setting mutation |
| `test_graph_type_path_capacity_precedes_segment_materialization` | Standard profiling observes refusal before path-vector rendering without replacing behavior |
| `test_graph_type_contract_round_trip` | Both database backends, scoped selection, verification after release and actual connection closure |

The issue/PR records commands, revisions, actual independent correctness/security/performance results and final-head supported-platform CI. Generic codec and existing source/schema regressions protect unchanged consumers. The retained master mutation audit covers pricing/CEL and supplies no type-section finding; no additional campaign is required.

Pure operations introduce no diagnostic sink or durable event. Existing type-declaration diagnostics retain their owner; failures contain fixed `GraphValueRejected` reasons rather than keys, paths, schemas or private values. Existing TM-01, TM-04/TM-10, TM-06/TM-07 and TM-08/TM-11/TM-12 boundaries remain applicable. Complete constructor activation diagnostics and public mapping remain #593 obligations.

The effective-output adapter still must select preparation's exact closure including inline dependencies, strip saved reference metadata, exclude types reachable only through inert recursive references and preserve the seven-member input ABI and independent refusal limits. Effective-output digests, checked CEL renderings/costs/frozen path sets, retry/resource facts and complete writer/reader composition remain #593 work. Complete provider facts and approval contexts retain #549/#552 ownership. No partial result activates deployment or execution.
