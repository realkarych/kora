# Remainder cohort balance, 2026-11-01

## Observed input

[CI run 41300834598](https://github.com/realkarych/kora/actions/runs/41300834598) requested head `07f61d1df4fe31a32095690f609d7c3b9d684c42` and tested merge checkout `3d5c709ab825a01ad26a28b34a01328e35c94a58` over base `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`. The macOS remainder job exceeded its ten-minute deadline. Its ordinary cohorts passed and both distributions were built, but cancellation prevented successful final admission. This remains a failed verification result.

The retained `scaffold-tests-macos-15-3.11.17-remainder` artifact reports:

| Cohort | Cases passed | Pytest wall time | Sum of JUnit case times |
|---|---:|---:|---:|
| measurements | 1,957 | 457.70 s | 447.762 s |
| integration | 779 | 386.15 s | 381.465 s |
| other | 8,331 | 446.86 s | 429.609 s |

`tests/plugins/artifact_type/test_agent_session.py` accounts for 207 cases and 36.721 s of the measurements case sum. `tests/ir/test_output_schema_resources.py` accounts for three cases and 9.206 s of the other sum. Test times include their existing observation overhead and process history; they are not portable benchmark guarantees.

## Approved bounded change

The owner approved the [placement and exact development-guide wording](https://github.com/realkarych/kora/issues/316#issuecomment-6557302579), with [approval recorded](https://github.com/realkarych/kora/issues/316#issuecomment-6557315422). Move the unchanged agent-session module into integration and the unchanged output-schema resource module into measurements. Keep three concurrent remainder processes, twelve scaffold jobs, isolated temporary/cache directories, every test and assertion, supported environments, evidence admission and all deadlines.

Arithmetic redistribution yields approximate case sums of 420.25, 418.19 and 420.40 s. This motivates balancing; it does not demonstrate wall-clock improvement. Collection, CPU contention, child-process scheduling and diagnostic RSS history can change. Exact release assertions remain blocking, while existing timing and memory observations remain diagnostic under their accepted methods.

The cohort architecture tests pin both memberships and exclusions and retain complete, disjoint inventory checks. Routing and evidence regressions and one complete changed-head CI establish the actual result. No unchanged retry campaign, timeout increase or coverage reduction is authorized by this record.

## Separate unresolved observation

The first #583 CI run on head `83be795` failed a four-way registry first-open with `registry_busy` at the existing two-second lock deadline. The [single authorized syscall trace](https://github.com/klukashka/kora/actions/runs/41300852484) tested `81d081f` on Linux/Python 3.15 and passed: the domain-file fsync took 3.551 ms and a contended lock succeeded about 12.7 ms later. It did not reproduce or explain the original delay. All local-runtime lanes passed on `07f61d1`; those passes do not establish a causal repair. Cohort balancing is unrelated to that observation and does not change its deadline.

## Accepted source-placement and subgraph integration

Run [41305211751](https://github.com/realkarych/kora/actions/runs/41305211751) tested requested head `f7fdf6b` through merge checkout `1f9bcad` over `4256789`. Its Linux/Python 3.11 remainder job exceeded the unchanged ten-minute limit. Preserved JUnit suites contain 1,899 measurements cases in 470.720 seconds, 987 integration cases in 424.820 seconds and 8,431 other cases in 517.879 seconds. All 11,317 cases passed without errors or skips, and each cohort recorded exit zero. The lane was nevertheless cancelled before offline builds and final synchronization; completed cohort tests do not satisfy complete lane admission.

The subgraph-interface resource module accounted for 48.633 seconds of summed case time in other. This is not a controlled savings estimate: case, suite and job durations differ, and shared-runner contention and earlier process history affect them. No individual failing assertion or hung test was identified in this artifact.

The owner requested integration of accepted master changes [#587](https://github.com/realkarych/kora/pull/587) and [#590](https://github.com/realkarych/kora/pull/590). Master `ab7e9bd` assigns unchanged source checks to conformance and leaves distribution builds in remainder; it assigns the unchanged complete subgraph-interface module to integration. Their union with this change preserves the separately approved agent-session and output-schema placements, all twelve jobs, complete/disjoint inventories, workload assertions and every execution deadline. New native destination evidence and complete final-head CI remain required; earlier independent passes do not guarantee capacity for the combined tree.
