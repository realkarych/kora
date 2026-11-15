# Graph parameter validation with the saved v2 profile

Implementation work under [#1612](https://github.com/realkarych/kora/issues/1612), part of the accepted [standard-schema replacement](../specs/schema-validation.md) and [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md). The complete publication/checking integration is implemented under #1612; final CI qualification remains in progress. The pure composition does not authorize a manual start, supply a locked accepting result or activate the deferred parameter consumers.

## Original source and immutable interpretation

`ir.standard_parameter_preparation` keeps the effective parameter root as the sole top-level constraint. Each selected named type remains a separate original document; equal roots are not deduplicated, and referenced types and their declared parents do not acquire additional assertion roles. The exact parameter profile is independent of the output policy. The existing two-member parameter binding and its interpretation are unchanged.

`engine.graph_parameter_evaluation` receives the completely checked detached graph data. It reuses the existing bounded parameter source arena and reference-selection machinery to check typed parameter pointers and select the exact saved closure, including inert definitions and compatibility references. The whole type inventory has already been checked by its owner. Name lookup, the whole-inventory marks array and selection scans are admitted separately from the selected schema certificate. An unrelated saved type never contributes a constraint or an executable reference root.

The transient source arena is released before standard source preparation. Its selected-name set and document wrappers borrow already owned graph values. The prepared result retains only canonical ASCII source bytes and scalar accounting; it retains no graph document, arena, selected record list, live registry or evaluator. An export or later mutation of the original graph cannot rewrite this holder.

The explicit publication binding freezes the exact ordered v2 table before construction. Reading dispatches from the saved profile; neither an output binding, a missing member nor a current default supplies parameter authority. The complete constructor requires this explicit selection and saves it immediately after `parameters`. The checking reader accepts the exact old eight-member or new nine-member envelope, checks identity, source, projections and physical resources, and attaches the prepared source to its immutable checked version. The old envelope returns no parameter holder. A missing, reordered, substituted or wrong-typed new binding cannot fall back to the old shape.

## Separate resource owners

The table below describes logical bytes and entries, not Python allocator bytes or process RSS. The common evaluator's per-row ledger remains unchanged; an unused allowance in one row cannot fund an unaccounted structure in another.

| Owner | Admission and release |
|---|---|
| Detached graph and saved type inventory | Existing complete acquisition/checking owner; its canonical values and native decoding retain their existing reservations. No database unit spans schema support or value evaluation. |
| Parameter source arena | A separate `ParameterReferenceBudget` admits its existing frames, sorted rows, native scalar spelling, nodes and sealing. Source construction does not run the retired custom evaluator. The arena is dropped before common source preparation. |
| Parameter reference selection | Its own budget admits typed pointer decoding, binary name lookup, whole-inventory marks and scanning, selected-name set and small wrappers. The selected source's node/byte count does not cover unrelated type records. |
| Prepared source | At most the accepted normalized source cap, including profile metadata; one canonical immutable byte string and scalar `SourceAccounting`. No full failed view is returned. |
| Original request | The existing request owner retains its original canonical bytes and identity. The original 4-MiB, signed-64 and root-zero depth32 envelope precedes profile evaluation. |
| Initial value and source decoding | The new synchronous composition caller reserves these before `request.parameters()` and source decoding. An original-envelope-valid value can exceed v2 node/cardinality bounds; this transient tree is not justified by the evaluator's admitted `2 * value_nodes` allocation. |
| Common evaluator | Existing exact v2 certificate, numerical view, registry and iterator/error disposal. Only a fixed decision and scalar accounting escape. |
| Deployment execution | Existing `DeploymentPreparer` and `PreparationDelivery`; `clear_preparation_error` disposes of unexpected chained/grouped exceptions. The existing slot remains owned until actual work and cleanup settle. Future parameter-request cancellation and accepting handoff remain separate activation work. |

`parameter_source_budgets` derives finite arena and selection allowances from the accepted source/node caps and the caller's already admitted type-section byte bound. The whole-reader/writer reservation must cover those allowances, preliminary native source scanning and standard support preparation together. No new table value or current-default compatibility rule is stored.

`parameter_request_reservation` separately accounts original canonical bytes, native input/decoder state and decoded source before the stricter profile scan. Its conservative outer count uses sixteen logical byte/entry units per original encoded byte for each of the value and source. This covers input/decoded text, scalar/container records, dictionary slots, transient pairs and the decoder's key memo; it is not a claim of sixteen physical bytes per byte of JSON. Its quadratic logical-work envelope covers parsing, scalar conversion and worst-case native key comparisons, in addition to the unchanged evaluator certificate. Independent resource review checked this mapping against the shared decoder and evaluator owners. These conservative operation bounds supply neither a process-heap nor an elapsed-time guarantee.

The complete checking reader reserves the larger output/parameter preparation peak because source checks run sequentially. It sums all work. Existing full graph decoding and canonical owners remain additive. An optional legacy `ParameterSchemaArena` is separately covered by the caller's explicit `ParameterSchemaReadLimits`; it may coexist with the new holder and is not included in the general reader reservation.

The writer retains completed facts and diagnostics while sharing the remaining transient allowance between CEL verification and schema preparation. Delivery can resume the writer before the native worker returns from its last request. Its persistent allowance therefore also includes one `ValidationWork` (bounded tree, rendering, bounds and projections) or `PatternProbe`, plus the fixed reply. This interleaving is not treated as complete native release. The program has already been destroyed by `Machine.validate`; full inventory traversal is no longer live. The explicit overlap bound is `max(typed_bytes + typed_work, 4 * MAX_PATTERN_PROBE) + FIXED_REPLY_BYTES`.

For existing deployment limits, the resulting writer reservation is 2,122,338,369 logical units and 30,717,149,129,658,595 work units; the detached reader reserves 857,740,832 and 4,055,337,795,589,717 respectively. The existing 2-GiB/2**55 writer and 1-GiB/2**52 reader settings are unchanged. Exact-bound and one-unit-short tests preserve admission as a blocking condition. These are logical ownership bounds rather than physical memory measurements.

An unexpected exception remains owned by its safe caller boundary. Raising an exception alone proves neither disposal nor release. The pure synchronous helper creates no queue, thread, process, cache or interrupt timeout.

## Disposition and diagnostics

A completed schema mismatch remains distinct from unsupported interpretation or exhausted admission. Publication of a well-formed unsupported parameter schema uses the existing safe-root `IR028` finding and deploy `API003`; incomplete support and inconsistent saved bindings use fixed `API010`. More-specific structural, dialect and reference findings retain precedence. POST `/validate` remains unimplemented and outside this work; its specified future mapping is unchanged.

Shared source and value operations retain their existing IR diagnostic owners and fixed outcome/reason/count fields. The actual API/deployment owner retains its terminal failure observation. Parameter values, keys, schema text and native error chains are excluded, including DEBUG. Diagnostic output cannot supply validation or execution authority.

## Verification status

At dedicated-composition commit `67862a1ef8cc7dcf6a1253d46edcef0d0a24d239`, 25 IR and 13 engine cases pass, as do 33 architecture checks and selected strict Ruff/format/Pyright checks. Cases cover original numerical identity, Boolean distinction, omission versus authored empty, local and named roots, inert reference closure, output-policy substitution, exact profile order/types, independent source ownership, safe diagnostics, the original request envelope and admission before decoding. These results do not qualify the complete saved writer/reader or an activated manual start.

The final PR must supply complete SQLite/PostgreSQL writer/readback/handoff evidence, rehashed malformed binding cases, existing authorization/replay and unavailable-start regressions, actual deployment refusal/lifetime evidence, independent implementation reviews and one successful complete final-head CI. No new timing/RSS campaign or mutation gate is introduced.
