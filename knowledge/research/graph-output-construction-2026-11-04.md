# Direct StructuredOutput construction and checking

[#672](https://github.com/realkarych/kora/issues/672), under #593, implements the [accepted leaf format](../specs/data-model.md#saved-structuredoutput-effective-contract) from #669. The [reviewed plan](https://github.com/realkarych/kora/issues/672#issuecomment-6616675105) preserves ADR-0018/0027 and introduces no public operation, migration, dependency, numerical default or execution authority.

## Actual sources and identity

`engine.graph_output_sources` selects the actual root or stored-body direct output from the private canonical IR snapshot. All four agent cases retain the accepted source/equality rule, human-input omission retains the exact default with its separate origin, and wait-event/command use their declared schema. Command requires the one additional output and an authored output-file field. Other node kinds, lists, missing selections and unrelated types refuse. Full source locations and extensions come from original IR, never the lossy diagnostic location conversion.

`engine.graph_outputs` checks the whole saved type section and real output consistency. It validates selected original inline schemas, preserves all general references and selected parents, and constructs the exact checker/ordered constraints and complete supported policy. The shared reference extractor retains named-type behavior and adds a null target type for an inline local reference. Original roots, object order and source paths remain distinct.

The operation invokes the real #667 preparation adapter. Its narrower executable closure and independent refusals remain authoritative; inert-only general dependencies never become executable assertions. On success only immutable canonical text and its 64-hex SHA-256 digest escape. The checking reader admits the saved value, verifies its digest and exact expected derivation against pinned IR, and returns saved interpretation unchanged. Recomputed hashes cannot repair a different source, schema, role, policy or target. Digest equality is not trusted publication or namespace authorization.

## Capacity and lifetime

The caller supplies a positive byte allowance covering complete IR text I, complete type-array text T, selector-array text P and effective record text R. I+T+P admission precedes decoding and source/type indexes. The reader admits saved R before expected derivation; saved and expected representations each have the same finite R allowance. They are separate buffers, not a claim of one physical copy.

Generated reference paths and sorting keys use a temporary meter no larger than the remaining R allowance, before path-vector/encoded-fragment growth. Their bounded text and decoded metadata are then included in final R admission. Selected records, ancestry and closure indexes derive from admitted I/T; final wrappers, inline source/schema/references, checker roles, ordered constraints and policy are incrementally charged. The actual preparation candidate is a subset of R and is separately admitted with I/T by the real adapter under the same caller allowance.

Each call has a constant number of bounded private snapshots/buffers. Temporary indexes, source records, references and the prepared holder stay call-owned and are discarded on return or refusal; only immutable text/digest survives. No database connection, task, process, thread or new cache is acquired. The caller excludes concurrent mutation while native inputs are captured. Existing general schema regex/cache limits remain; encoded size is not a heap/RSS, CPU-work or wall-time guarantee, and no new interrupt promise is made.

Whole-graph output consistency and type/preparation checks repeat per leaf. The full consumer must bound output count, repeated work, retained values/holders, frequency and concurrency and meet its existing NFR-3 gates. This component does not establish a 1,000-node operational envelope or safe aggregate publication.

## Blocking evidence and remaining activation

- `test_graph_output_agent_source_cases`, `test_graph_output_explicit_empty_schema_is_authored` and default-origin/direct-omission cases exercise actual sources in root and stored-body definitions, equal reordered schemas, empty authored objects and provenance-bearing identity.
- General-closure and inline-remote tests verify custom checker/constraint roles, unused and inert dependencies, typed escaped/local targets, distinct equal roots and real preparation. Unsupported schema and document-capacity tests preserve actual preparation refusals.
- Rehashed-substitution, digest, source-consistency and wrong-selection tests reject missing/extra/reordered/mismatched saved meaning and source claims without content-bearing errors.
- Aggregate-admission and generated-reference tests separate input refusal before dependent parsing/indexing from output-path refusal before vector materialization. Exact capacity succeeds; one-under refuses. Later caller mutation cannot change returned identity.
- `test_graph_output_contract_round_trip` uses the real physical graph-value codec, metadata table, owner-scoped reader and shared SQLite/PostgreSQL fixture for all four producers. The leaf is a partial test payload in the physical column, not the complete outer graph format. It checks original scalar kind, signed zero, NUL, large supported integers, source/order/targets/digest, foreign-namespace refusal, actual read-unit closure before checking and final connection closure.
- `test_graph_output_diagnostics_exclude_retained_content` composes existing content-free IR/preparation events under DEBUG and verifies source/schema content and stdout exclusion on success, refusal and invalid source. Pure identity helpers add no sink or operational/audit event; the complete caller retains unexpected-failure and deployment reporting.

The PR records exact commands, tested revisions, actual local/CI outcomes and independent correctness/security/performance results. Tests described here do not imply that an unexecuted platform check passed. Final-head Linux/macOS CI and both database environments remain mandatory; the whole-workflow ten-minute feedback target is reported separately from hard deadlines. No additional mutation or numerical campaign is introduced.

Graph-wide output binding/inventory, other native artifacts, lists/wrappers/exports, expressions/projections/retry/resource sections, trusted provider facts, native effective-schema delivery, full writer/reader and server start admission remain #593 obligations. The reserved evaluator/common composition retains #138 ownership. This leaf cannot publish an executable graph or accept an artifact.
