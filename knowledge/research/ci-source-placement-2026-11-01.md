# Separate source checks from the long remainder lane

Date: 2026-11-01. Implementing issue: [#586](https://github.com/realkarych/kora/issues/586). The owner approved the [bounded source-only placement and exact development-guide sentence](https://github.com/realkarych/kora/issues/586#issuecomment-6557488820), with [direct approval recorded](https://github.com/realkarych/kora/issues/586#issuecomment-6557514278). This revises only the source-check part of accepted #301; offline distribution builds remain in remainder.

## Retained failure and scope

PR #567's [run 41301031919](https://github.com/realkarych/kora/actions/runs/41301031919), head `ae8bb1844f68a795821fcb579fa7dcfac3f300b4`, exceeded the unchanged ten-minute Linux/Python 3.11 job deadline. Job `124832328548` lasted 609 seconds: initial setup/frontend took 20 seconds, source checks 60 seconds, and the test step was cancelled after 522 seconds. The preserved cohort results report 8,528 other cases in 517.05 seconds, 2,103 measurement cases in 474.96 seconds and 780 integration cases in 390.67 seconds, all without failures, errors or skips. Successful test reports and upload do not qualify the cancelled job: distributions and final synchronization did not execute, and lane completion failed. The original failure remains failed.

The existing source/frontend commands move from remainder into conformance on all three platform/interpreter rows. Each conformance completion guard requires successful source checks; each remainder guard requires that step skipped and distributions successful. Source failure, cancellation, skip or missing result still rejects the owning lane and downstream shared evidence, protected Scaffold and CEL joins. The same twelve lanes retain all test selections, five cohorts, child joins, isolated temporary/cache roots, logs, exit files, JUnit identities, receipts and required statuses. No test, workload, process, job, permission, action pin, dependency, platform, database or hard deadline changes.

## Diagnostic planning scenarios

Existing natural runs `41301030846`, `41301077753` and `41301031919` supply the observations below. Their feature revisions differ; shared hosted load, cache/preconditioning and process history prevent treating arithmetic as a calibrated prediction or passing performance guarantee.

| Environment | Maximum observed conformance job | Maximum observed source step | Arithmetic conformance scenario |
|---|---:|---:|---:|
| Linux / Python 3.11 | 323 s | 60 s | 383 s |
| Linux / Python 3.15 | 328 s | 59 s | 387 s |
| macOS / Python 3.11 | 360 s | 84 s | 444 s |

For the interrupted remainder, `609 - 60 + 16 = 565` seconds is an engineering scenario only. The 16 seconds are an allowance for unexecuted build/finalization, not measured missing work; observed successful distribution steps in the inspected samples took 8–9 seconds. No new timing gate, workload, instrumentation or repeated campaign follows from these calculations. One complete successful final-head native execution must pass all existing hard job/test/cleanup gates. Whole-workflow duration, including queues and dependent jobs, remains diagnostic against the 600-second feedback target, with existing maintenance owners #527 and #328.

## Integrity and verification

Existing TM-12 covers CI/contributor evidence integrity, with TM-08 supporting revision/source provenance. The same producer result and run/attempt/revision binding remain authoritative. Actors, credentials, permissions, content boundaries, destinations and required guarantees are unchanged, so no threat-model amendment is needed. There is no new executable product component or diagnostic event.

The existing `test_scaffold_protected_names_belong_only_to_complete_joins` pins separate source/build owners. `test_lane_completion_rejects_every_incomplete_outcome` preserves executable failure/cancellation/skip/missing-result refusal, including the unexpected execution of a non-owning step. Existing complete/disjoint inventory, receipt/hash and shared-join tests remain unchanged. The PR records strict/architecture checks, independent correctness/security/performance design and actual-source results, exact final revision and native execution evidence. No successful receipt substitutes for a missing build, final-sync or job result.

Revisit a new hard failure, missing evidence, or natural-run headroom loss with a concrete cause. Two unsuccessful repairs of the same required failure require owner decision. No unchanged favorable rerun or further placement, gate, workload or resource change is authorized by this record.
