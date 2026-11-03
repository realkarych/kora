# Graph parameter section construction and checking

Implemented under [#660](https://github.com/realkarych/kora/issues/660), a prerequisite of [#593](https://github.com/realkarych/kora/issues/593), against base `a31fcd1500aa0a7cee50b96e342049326a454582`. The [implementation plan](https://github.com/realkarych/kora/issues/660#issuecomment-6614581099) applies the already accepted [parameter binding](graph-parameter-binding-2026-11-03.md) and [canonical physical representation](graph-value-storage-2026-11-03.md). It makes no new public or persisted format decision.

## Delivered boundary

`build_graph_parameter_binding` extracts only the pinned `GraphDocument.graph.params`, substituting the exact accepted closed-empty schema when omitted. Authored `{}` remains permissive. The result is immutable compact ASCII canonical text of the parameter member. It is neither a complete contract nor proof that the schema, graph, provider installation or execution is valid.

`read_graph_parameter_binding` accepts an untrusted decoded member and the pinned structural IR. It requires exactly `interpretation` and `schema`, supports only `kora.graph-parameters.v1`, requires an object schema and compares its canonical text with the expected effective schema. Both outer-member orders are accepted and preserved; schema order, numeric kinds and signed zero remain canonical identity. Astral characters and their materialized surrogate pair share canonical identity; decoded-name collisions refuse. Original schema roots, reference spelling and annotations are retained without resolution or defaults. Omission is not inserted into IR.

The structural document and its caller-owned mappings must remain unchanged during each synchronous call. Mutable post-parse data is rechecked: non-JSON values, cycles and repeated containers within the parameter field refuse. Other independent open fields keep their existing ownership; the shared physical codec's default behavior still accepts aliases across fields. A narrow `reject_aliases` renderer option retains visited identities only for callers requiring per-field refusal.

## Bounds and resource ownership

Both operations require an explicit trusted positive `max_encoded_bytes` for the complete canonical member. No public numerical default is selected. Before adapting the exact native `IrJsonObjectField` root to a plain dictionary, the builder reserves fixed wrapper syntax and bounds cardinality: a nonempty n-member object needs at least `5n + 1` bytes, and an empty object needs two. Base dictionary operations copy references only after admission. Descendants are rendered iteratively; scalar expansion and traversal state use the existing bounded codec. The seen-container set grows only with admitted container syntax.

The reader renders before parsing or comparing saved contents. Every individual representation and intermediate traversal is bounded by the supplied capacity; multiple capacity-derived temporaries coexist. This is not a Python-heap-equals-budget, process RSS, latency or throughput guarantee. GraphDocument construction, raw database acquisition and public whole-contract limits remain outside this helper. Full composition must own an aggregate finite compatible budget before activation. Neither helper acquires a connection, file, task, process or provider resource.

The integration test composes the real physical codec and raw namespace-scoped lookup: prepare before entering the writer transaction, release read units/checkouts before decoding and verification, exercise mismatch refusal outside the read context, and observe actual connection closure. This test-owned composition does not implement a production graph-version writer or permit a partial contract to execute.

## Verification and activation

| Test | Distinct obligation |
|---|---|
| `test_graph_parameter_binding_preserves_authored_and_omitted_schema` | Exact effective values, original IR, references/annotations and saved outer order |
| `test_graph_parameter_binding_refuses_inconsistent_saved_values` | Missing/extra/unsupported shape and unsafe or substituted schema refuse content-free |
| Numeric and Unicode identity regressions | Canonical scalar distinctions, equivalent Unicode and collision refusal |
| `test_graph_parameter_binding_is_bounded` | Exact and one-over byte capacity for escaped scalars, huge integers and deep containers without process-setting changes |
| Root admission regression | Standard cProfile observation proves no root copy or member traversal before wide-root budget refusal; no replacement behavior |
| Post-parse alias regression | Repeated container and cycle mutation cannot bypass the original per-field admission rule |
| `test_graph_parameter_binding_round_trip` | SQLite/PostgreSQL persistence, namespace isolation, success/refusal after unit release and actual connection closure |

The issue and PR record actual revisions, commands, independent correctness/security/performance results and final-head CI. Source-review conclusions and executable checks remain separate. Existing generic-codec regressions protect unchanged default alias behavior. No new numerical benchmark, mutation campaign, CI policy or acceptance relaxation is introduced. The applicable retained master audit has pricing/CEL scope and no parameter-binding finding.

Pure operations introduce no diagnostic sink, entry point or durable event. Fixed `GraphValueRejected` reasons contain no key, schema, private value or reference text. Existing threat-model controls TM-01, TM-04/TM-10, TM-06/TM-07 and TM-11/TM-12 apply without a new actor or authority. Complete-constructor activation diagnostics, public error mapping, current authorization, complete type/output closure, checked expression/projection records, resolved retry/resources, saved identity verification and deployment/start integration remain #593 obligations. Complete trusted provider facts and approval contexts retain #549/#552 ownership. The parameter result alone satisfies none of those activation gates.
