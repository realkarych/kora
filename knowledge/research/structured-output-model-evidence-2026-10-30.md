# StructuredOutput standalone model evidence

Date: 2026-10-30. Implementing issue: [#446](https://github.com/realkarych/kora/issues/446), a child of [#138](https://github.com/realkarych/kora/issues/138), MVP first-slice work item 2. The project owner approved the [concrete plan and exact scoped human-document wording](https://github.com/realkarych/kora/issues/446#issuecomment-6508537813). The [published revision-1 contract](../specs/artifacts/StructuredOutput.md), schema, descriptor and vectors retain their immutable identities.

## Delivered boundary

The standalone model admits exact builtin native JSON values and strict raw bytes, stores owned canonical bytes in a frozen slots value and returns detached public values and summaries. Canonical/digest/summary operations revalidate unchecked state. Native admission checks container cardinality, string size and integer magnitude before proportional copies, encoding or formatting; every occurrence of an acyclic shared container consumes budget. The snapshot, rather than the source, feeds sorting and encoding. Raw admission bounds bytes and lexical depth before decoding and preserves exact integers and finite binary64 values.

This boundary owns bounded synchronous temporary memory. It opens no file, connection, task, thread or provider. It adds no persistent cache or asynchronous cancellation point. Custom effective-schema arithmetic and work bounds, CEL/producer integration, ArtifactType provider, API/upload/replay/storage/retention and derived-list activation remain pending their named implementing owners. A JSON array value here is not an activated derived list contract.

## Verification method

Behavioral tests exercise all 39 frozen canonical cases and 18 raw-refusal cases, both signs of the integer-magnitude boundaries, exact root kinds, source/export mutation, unchecked state, aliases versus cycles, raw/canonical expansion and content-free failures. Native hostile-input tests compare incremental traced allocation with the already allocated input container/string/integer size: the specific claim is refusal before duplicating that whole oversized input, not a process-wide peak-memory guarantee. Source review also checks allocation order.

The existing IR action harness adds 38 named workloads, three warm-ups and ten samples per action. It covers raw/native/canonical/digest/summary phases at 1 KiB, 64 KiB and near/exact 1 MiB; native integer/depth/canonical edges; NUL and supplementary Unicode; oversized containers/strings/integers; shared-reference expansion; and near-raw-cap float mantissas/exponents, successful rounding and late failures.

The existing lifecycle harness adds three serial traced/untraced pairs of 500 actions: each child executes twenty batches of twelve successes, twelve refusals and one rotating stress action, yielding exactly 248 successes and 252 refusals. Twenty-one settled checkpoints preserve descriptors, Python/OS thread counts, tracing/native observations and two retaining-path summaries. Each child retains the existing ten-second deadline and bounded output. Caller-retained inputs/results are distinct from disposable operation temporaries.

The additive IR inventory contains 366 test identities, 1,271 proof entries and 11,054 samples. The previous 359/1,227/10,668 remain present; the new seven tests, 44 proofs and 386 samples run exactly once in existing shard 3. No prior workload, platform, numerical gate, hard deadline or lane is removed. Reader regressions reject missing or changed manifests, wrong outcomes/revision, incomplete transport and leaked resource counters for either tracing mode.

Product size/depth/integer admission, exact outcomes, immutable identity and actual resource release remain blocking. The named [approved verification audit](approved-verification-audit-2026-10-29.md) governs diagnostic IR heap comparisons; the [bounded-MVP amendment](bounded-mvp-verification-2026-10-29.md) separately governs repeated qualification. Elapsed time, RSS and post-GC deltas are observations with retained signed values, not passing latency/leak guarantees. No new numerical acceptance method or campaign is introduced.

## Current observations and limits

An initial local macOS/Python 3.11.17 execution passed 285 focused model/publication/VcsRef tests in 2.71 seconds. The new seven measurement tests passed in 14.33 seconds on the local shared host. That initial invocation omitted the optional report-output setting, so it supplies pytest execution evidence without retained numerical samples; final-revision CI artifacts must supply the complete raw observations and receipts. Neither local result proves Linux execution or whole-product activation.

Initial integration checks passed 380 cases and failed two additive inventory/routing assertions: the hardcoded complete union still named the old totals, and literal workflow selectors assigned new tests to the default shard. The implementation updates those totals and the two existing selector lines while preserving the exact union; the affected reader/architecture selection then passed 397 cases in 21.06 seconds. Historical failures remain failures.

Independent actual-source assessment and all mandatory final-head CI are pending at this record's initial revision. The implementing PR records their actual outcomes, inspected revisions and artifacts before readiness. The immutable published contract checks remain separate from model evidence and from future runtime integration.
