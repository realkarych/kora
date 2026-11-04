# Saved graph type adaptation to schema preparation

Implemented under [#667](https://github.com/realkarych/kora/issues/667), following the [type-section implementation](graph-type-construction-2026-11-04.md) and accepted [preparation boundary](../specs/data-model.md#saved-type-closure-section). This pure composition returns the real opaque prepared holder or its refusal. It supplies no saved effective-output format, payload truth result, provenance or execution authority.

## Inputs and ownership

`engine.graph_schema_preparation.prepare_graph_schema` receives canonical complete IR text, an untrusted saved type section, a private candidate object and an explicit positive aggregate byte budget. The candidate contains exactly `declared_type`, `inline_schema`, `execution_semantics`, `checker_binding` and `schema_validation`. Inline is null or `{source,schema}` with an exact native Boolean or object schema root. The complete policy is explicit; missing fields never inherit current defaults. This call envelope is neither a persisted representation nor a public API.

The complete writer/reader remains responsible for selecting the real output and proving the inline schema/source's provenance. The adapter checks consistency and preparation of the supplied candidate only. Source metadata is inert content; it authorizes no read, import, diagnostic content or effect. Boolean false is a valid prepared constraint, not an evaluated mismatch.

## Admission, closure and real composition

Let I be the complete canonical IR ASCII text length, S the canonical type-array text length and C the canonical candidate-object text length including its wrappers and fields. I+S+C must fit the caller's explicit budget before decoding, type checking, indexes or closure construction. The existing codec's fixed array wrapper has six local syntax bytes, excluded from global array accounting. Exact aggregate capacity is accepted. Privately decoded snapshots prevent caller mutation from changing admitted meanings.

The real type reader checks the section against pinned IR and native sources. The real policy binder checks all thirteen literal parameters and supplies its exact ten preparation parameters. Object inline roots receive actual general keyword/reference checks against the verified saved roots. Boolean roots have no keywords or references to scan. General checks preserve their existing regex/cache limitations.

Closure selection starts with the declared type and its parent chain plus inline-schema dependencies. It follows all schema-position `$ref`/`$dynamicRef` targets, including unused definitions, and required parents recursively. Each selected type is queued once; references are never expanded. Unrelated declarations and types reachable only through inert `$recursiveRef` are excluded. Inline dependency discovery streams recognized positions without constructing a second path/reference inventory. Source/target metadata is used only after actual type verification.

The adapter emits canonically name-ordered seven-member preparation records by removing saved general-reference metadata. Original schemas, descriptors, behavior and source records remain intact. Inline remains a separate original document. The actual preparer rederives executable targets, ancestry, constraint/checker roles and call graph; reference-only dependencies do not become top-level constraints. Its independent UTF-8, unsupported-keyword, cycle, depth/document/work and other resource refusals remain effective. Derived lists retain the current refusal.

Adapter indexes, closure state and copies derive from admitted I/S/C; several constant-factor temporaries coexist. This is no process heap, latency or linear-time guarantee. Adapter scratch ends with the call, and the caller owns the returned opaque holder. Whole-consumer concurrency, repeated preparation and aggregate holder retention remain separate activation gates. No database, process, task, provider or network resource is opened.

## Failure and observability ownership

Malformed representation, inconsistent saved types and invalid general inline references raise existing fixed `GraphValueRejected`; aggregate exhaustion uses its `BUDGET` reason. The real policy binder retains `EvaluationRefused(unsupported_policy)`. Actual preparation retains its existing `unsupported_binding`, `unsupported_schema` and `preparation_exhausted` results. No refusal is converted to schema-false or partial success. Programming errors propagate to the caller-owned boundary; no public status or error mapping is selected here.

Actual preparation retains its existing content-free completion event. The adapter adds no sink or durable event. Complete output/constructor activation diagnostics remain #593. Existing TM-04/TM-10, TM-06/TM-07 and TM-08/TM-11/TM-12 cover input capacity, safe failure and authority separation.

## Verification and remaining consumers

`test_graph_schema_preparation_selects_exact_closure` composes actual type construction/checking, adapter and preparer; checks original roles/documents, transitive and inline-only dependencies, inert/unrelated exclusion and no retained caller containers. Boolean-inline and detachment tests protect original roots and holder ownership. Real-refusal, explicit-type/binding/policy/input tests cover unsupported schemas/cycles/surrogates/lists, absent or malformed selection, saved substitution and invalid inline references. Independent document/depth/work exhaustion remains observable. Aggregate exact/one-under profiling checks admission before decoding/checker/index construction.

The issue/PR records actual revisions, focused and relevant regressions, strict static checks, independent correctness/security/performance results and complete supported-platform CI. There is no new numerical benchmark or acceptance relaxation. Retained applicable master audit reports concern pricing/CEL, not this adapter, and add no campaign. Actual persistence/release remains covered by #664; this operation performs no database access.

Complete node/inline selection, effective-output records and digests, CEL estimates/frozen paths, retry/resource sections and whole-version writer/reader remain #593 work. The reserved evaluation policy remains unavailable until its handlers, adapter and common composition satisfy #138/ADR-0027 activation. Preparation success alone satisfies neither those gates nor installation validation, publication or graph execution.
