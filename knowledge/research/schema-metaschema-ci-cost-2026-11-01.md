# Schema-map metaschema cost in required CI

Date: 2026-11-01. Implementing issue: [#591](https://github.com/realkarych/kora/issues/591). The owner approved the [bounded implementation plan](https://github.com/realkarych/kora/issues/591#issuecomment-6558406035), with [explicit approval recorded](https://github.com/realkarych/kora/issues/591#issuecomment-6558445802). Baseline: `d0fed99becd82205af4e7a6d546d1484536599f2`. Final-revision commands, environments, independent implemented-source results and native CI evidence belong to the issue's pull request.

## Retained CI diagnosis

[Run 41307815595](https://github.com/realkarych/kora/actions/runs/41307815595), attempt 1, requested head `44e8e517373e71e63e881d665501b099905dda56`, completed successfully in 977 seconds from workflow creation through its last completed job. Its critical path separates pre-start waiting from actual job execution:

| Job | Created to started | Execution |
|---|---:|---:|
| Scope classification | 67 s | 8 s |
| Linux / Python 3.15 remainder | 274 s | 507 s |
| Scaffold evidence | 78 s | 10 s |
| CEL aggregate | 13 s | 18 s |

Waiting totals 432 seconds; job execution totals 543 seconds; creation and dependency gaps account for two seconds. These timestamps establish waiting, not an account concurrency limit, runner scarcity or a demonstrated benefit from serializing publication. [#527](https://github.com/realkarych/kora/issues/527) retains scheduling attribution, and [#328](https://github.com/realkarych/kora/issues/328) retains lane headroom.

[Master run 41307651560](https://github.com/realkarych/kora/actions/runs/41307651560), attempt 1, revision `98cb7e947d574872284accc72c880ee961980d87`, completed its last job 642 seconds after workflow creation. Its final API update was one second later. Linux / Python 3.15 remainder JUnit reports 2,103 measurement cases in 450.379 suite seconds, 8,615 other cases in 452.981 seconds and 780 integration cases in 398.129 seconds. The inline-schema and saved-type-schema observation cases took 74.881 and 65.199 seconds respectively, including their existing tracing work. Both observations retained their complete 21/18 timing samples and six memory batches; all four release cases passed.

[PR #590 run 41308740266](https://github.com/realkarych/kora/actions/runs/41308740266), attempt 1, head `f2bdb627840f4aaa456ba934c191cb5875a8589f`, completed all 40 jobs successfully in 730 seconds. That separate unchanged-module placement is not implemented or qualified by this change. Different source inputs, hosted contention, caches and process histories prevent calibrated before/after comparisons between these runs.

## Selected source mechanism

`map_value` already prepares a complete operation-local `SafeObject`. Previously, `KeywordValidation.check` created a singleton map for each entry and ran that singleton through the complete pinned draft-2020-12 metaschema. A wide map therefore repeated the outer validator's vocabulary traversal for every entry.

The implemented allowlist batches exactly `$defs`, `definitions`, `properties`, `dependentSchemas`, `dependencies`, `dependentRequired` and `$vocabulary`. Each prepared map is passed once to the same `check_value`, real pinned metaschema, offline registry and regex-only format checker. Authored keywords remain independent. `patternProperties` and future unreviewed map keywords keep singleton validation: a regex compiler capacity exception must not terminate checking of subsequent entries.

For the seven allowed maps, preflight already records malformed entries independently. Schema children become valid Boolean placeholders; dependency arrays become unique safe-text arrays or valid empty placeholders after reporting their faults; vocabulary entries become Boolean placeholders. Their prepared forms have no additional failing entry constraint under the pinned metaschema and explicit checker. Consequently, batching preserves validity, fixed IR022 findings, safe paths, diagnostic ordering and omission counts. Iterative child traversal and offline reference checks still inspect the original schemas. Both saved-type and inline callers retain their counters, immutability, completion events and activation limits under [the IR contract](../specs/ir.md#10-validation-rules-kora-validate).

This reduces repeated constant work, not asymptotic time complexity. Deep single-child chains and many independent roots receive no equivalent reduction. Empty maps gain one bounded native check. No cache, dependency, quota, executable binding, public operation or workflow change is introduced.

## Memory and diagnostic limits

The prepared map already exists before native validation, but pinned `jsonschema` creates `extras = set(...)` while checking additional properties. A batched map therefore introduces a temporary map-width key set where singleton validation used singleton sets. Total temporary space remains linear in prepared input; equal or lower peak bytes are not claimed. The native error generator remains incrementally consumed inside `closing`, and all prepared containers and native traversal state remain call-local.

The initial bounded local profile used macOS / Python 3.11.17 and locked dependencies at the baseline. It warmed the existing `roots_1000`, `depth_1200` and `shared_refs_1000` inline workloads once, then profiled one additional validation of each with `cProfile` and tracing disabled. The hypothesis was that generic metaschema traversal dominated representative inline validation; the decision criterion was more than half of cumulative validation time in the native validator path. Observed profiled time was 1.416 seconds; `iter_errors` accounted for 1.392 cumulative seconds across 4,201 calls. Per-workload profiled times were 0.005, 0.511 and 0.907 seconds.

Those counts are coordinator-observed diagnostics from the actual command/stdout transcript. A separate raw profile artifact was not saved before review, so the independent performance reviewer did not verify those counts. The source mechanism and native JUnit were independently inspected. No rerun was introduced solely to change the profile's provenance limitation. Profiler overhead, disabled tracing and different process history prohibit a CI-speedup or future-headroom prediction.

## Verification boundary

The [actual independent design results](https://github.com/realkarych/kora/issues/591#issuecomment-6558417595) cover correctness, security and performance for this exact seven-keyword boundary. Existing TM-04, TM-06/TM-07, TM-10 and TM-12 cover untrusted schema admission, content protection, temporary retention and dependency/evidence integrity. Actors, authority, accepted behavior and required guarantees remain unchanged, so no threat-model or normative-contract amendment is needed.

Focused regressions compare empty and wide maps against the real metaschema, preserve independent malformed-entry and dependency-array findings, assert canonical diagnostic paths/order/omissions and input immutability, and retain later regex findings after compiler refusal. An inline regression additionally checks the shared helper's masked paths and source ownership. Both existing schema resource modules remain byte-for-byte unchanged: every workload, warm-up, 21/18 timing samples, six traced batches, counter, weak-reference and zero-view assertion, and JUnit property remains mandatory.

One successful complete final-head native CI, existing strict checks and applicable independent implemented-source review remain required. Linux / Python 3.11 and 3.15, macOS / Python 3.11, and SQLite/PostgreSQL coverage retain their existing owners. The complete workflow duration remains diagnostic against 600 seconds; job, test, resource and cleanup deadlines remain hard checks. Historical failures keep their outcomes. No additional campaign, unchanged favorable rerun, raised timeout or reduced assertion follows from this optimization.
