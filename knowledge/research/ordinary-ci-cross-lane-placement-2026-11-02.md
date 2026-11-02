# Ordinary CI placement across general lanes — 2026-11-02

## Scope and decision

[Issue #632](https://github.com/realkarych/kora/issues/632) moves four complete ordinary test modules into the existing first conformance process. The project owner [approved the bounded plan and exact development-guide wording](https://github.com/realkarych/kora/issues/632#issuecomment-6574913138) on 2026-11-02. The [implementation plan](https://github.com/realkarych/kora/issues/632#issuecomment-6574809382) defines acceptance; its independent design review is recorded [with actual scoped results](https://github.com/realkarych/kora/issues/632#issuecomment-6574837291).

| Module | Previous group | Destination |
| --- | --- | --- |
| `tests/architecture/test_ir_measurement_ci.py` | integration | first |
| `tests/ir/test_structured_schema_assertions.py` | integration | first |
| `tests/measurements/test_ir_measurement_evidence.py` | measurements | first |
| `tests/architecture/test_no_suppression.py` | other | first |

The move preserves twelve scaffold jobs, five general processes, the three platform/interpreter combinations, database selection, test contents, fixtures, numerical methods, assertions, process joins, isolated temporary/cache roots, logs, exits, JUnit, receipt formats, protected statuses and hard deadlines. Evidence tests move; the instrumented measurement workloads retain their existing placement. Source checks remain in conformance and offline distribution builds remain in remainder. No dependency, permission, package or product behavior changes.

The workflow owns literal cohort membership. `test_ordinary_cohorts_partition_the_general_lane_files` pins the destination and preserves the complete, disjoint general-suite partition. The existing scaffold evidence validator independently requires complete current execution, successful identities and matching provenance. TM-12 check integrity and TM-08 provenance apply; no threat actor, authority-bearing flow or admission rule changes.

## Historical native evidence

[CI 41424269685, attempt 1](https://github.com/realkarych/kora/actions/runs/41424269685/attempts/1) requested `95db3637eb87b79369f889745987eb744f6f60a8` from PR #631 and checked out merge revision `73ddee23ac5c3ffb982007d0eb2ea08ac2a92b02`, whose parents are base `04eb46fcff90539e33435e9f691cb55d446c2d54` and that requested head. The merge checkout and requested head share tree `0b39b890a614a30a4c1ea28cdbe2ce3b1338502b`.

The [macOS remainder job](https://github.com/realkarych/kora/actions/runs/41424269685/job/125207027869) waited 376 seconds and then took 616 seconds. GitHub cancelled it for exceeding the hard 10m0s job limit. Its 11,834 retained cases passed without failures, errors or skips, but the cancelled job and failed downstream admission remain non-passing. Whole-workflow duration was 1,080 seconds, exceeding the ten-minute feedback target by 480 seconds. Queue delay is separate from execution time; the hosted-capacity notice does not establish the cause of the execution overrun.

The macOS conformance job took 431 seconds; its Linux counterparts took 449 and 411 seconds. Retained macOS artifacts `scaffold-tests-macos-15-3.11.17-remainder` (artifact 13131862865) and `scaffold-tests-macos-15-3.11.17-conformance` (artifact 13132376280) bind the same run, attempt, checkout and requested revision. All twelve receipt file hashes in each artifact matched. Every recorded exit was zero. Retained JUnit results were:

| Group | Cases | JUnit duration |
| --- | ---: | ---: |
| integration | 1,205 | 507.041 s |
| measurements | 1,818 | 467.064 s |
| other | 8,811 | 467.896 s |
| first | 231 | 205.466 s |
| second | 504 | 294.872 s |

These artifacts establish their retained test outcomes, not successful workflow qualification or new-head performance. The [failure record](https://github.com/realkarych/kora/pull/631#issuecomment-6574590717) preserves the native result.

## Bounded rationale and limits

The four moved modules account for 792 existing cases and case-time sums of 33.900, 29.283, 21.193 and 17.597 seconds in the table's order. Subtracting them from their old groups and adding them to first produces a purely arithmetic scenario: remainder groups 443.858 / 445.871 / 450.299 seconds and conformance groups 307.439 / 294.872 seconds.

Those sums are not portable isolated costs or a forecast. Fixture attribution, process history, caches, concurrency and hosted contention can change actual duration. Linux per-module timing was not inspected. The scenario justifies a bounded placement attempt; it creates no new performance guarantee or numerical gate. The original cancelled run is never reclassified.

The implementing PR records its actual tested revision, native CI and artifact links, source/destination cohort and job durations, queue durations and whole-workflow feedback result. One successful complete final-head CI, focused selection/admission regressions and independent actual-diff reviews are required. Missing or failed required evidence blocks merge. No unchanged campaign is repeated to obtain a favorable result.

[Issue #527](https://github.com/realkarych/kora/issues/527) retains whole-workflow scheduling diagnostics; [issue #328](https://github.com/realkarych/kora/issues/328) retains destination headroom and lifetime-guard obligations. A first-group lifetime-guard failure attributed to or unresolved under the changed contention or process history returns to the owner before any repair or another run. The ordinary two-unsuccessful-repair allowance does not override that condition. Further changes to scope, resource ownership or gates require owner approval.

The available master mutation audit 41297342266 at source `6b6d2b` supplied only partial pricing/CEL execution and no applicable CI-placement finding. It adds no PR campaign or merge gate.
