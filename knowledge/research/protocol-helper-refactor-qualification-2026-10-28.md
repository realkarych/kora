# Protocol helper refactor qualification scope

Historical disposition, condensed 2026-11-01. [PR #340](https://github.com/realkarych/kora/pull/340) completed issue #393. The [immutable original](https://github.com/realkarych/kora/blob/c407647ee16e4969e934479f1c3a6cb197dd3e95/knowledge/research/protocol-helper-refactor-qualification-2026-10-28.md) retains exact source/digest comparisons, independent-review provenance and raw cohort observations.

## Accepted scope and current owners

The refactor consolidated protocol, JSON, UTF-8, refusal and CEL-bound helpers while preserving validation results, errors, wire bytes and storage digests. The owner-approved [acceptance amendment](https://github.com/realkarych/kora/issues/393#issuecomment-6472140342), with [approval](https://github.com/realkarych/kora/issues/393#issuecomment-6472193800), removed only this refactor's extra fresh storage-qualification merge prerequisite. It did not qualify a release or grant a general exception for later changes.

Current merge acceptance follows the accepted [bounded MVP amendment](bounded-mvp-verification-2026-10-29.md). The [storage qualification schedule](storage-qualification-schedule-2026-10-26.md) and [development guide](../../docs/development.md) retain successful complete qualification for the exact release revision, all ordinary integrity/replay/cancellation/resource checks, workloads, instrumentation, numerical assertions and hard child/job/cleanup deadlines. No new runtime or policy change is made here.

## Failed qualification and remaining limits

[Run 40688568316](https://github.com/realkarych/kora/actions/runs/40688568316) tested `2280336441810667d623af6c88aab92d73438a65`. macOS cohort 0 exceeded its unchanged 480-second child deadline and was killed after 829 of 832 checkpoint rows; cohorts 1 and 2 completed all rows. The final 256 MiB alias workload completed no measured cycle in cohort 0. Zero disposable resources at earlier checkpoints do not establish interrupted-cycle closure or final shutdown; the complete qualification remains failed.

Independent source inspection established no demonstrated refactor-caused regression or dominant avoidable cost; it did not identify the timeout's cause or prove a sufficient fix. Closed [issue #396](https://github.com/realkarych/kora/issues/396) retains the subsequent attribution history. Its closure does not relabel this failed run or qualify a different release revision. The separate pressure-fixture detach defect remains distinct under [issue #302](https://github.com/realkarych/kora/issues/302). No lifetime, latency or leak-freedom guarantee follows from this summary.
