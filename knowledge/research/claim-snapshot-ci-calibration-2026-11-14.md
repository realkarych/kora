# Claim snapshot CI calibration, 2026-11-14

This record supports [issue #1532](https://github.com/realkarych/kora/issues/1532) and the existing [duration refresh procedure](../../docs/development.md#ci-and-contributions). Only generated estimates change. The planner, three children, deterministic 270-second planning bound, eight-lane maximum, hard ten-minute job deadline, required tests, environments, permissions and statuses remain unchanged. Estimated and observed wall times remain diagnostic; the deterministic planner checks remain blocking.

## Failure and maintenance trigger

Master `4c82de454f2637ec23fb2e185a0ab69957f2d8fd`, [CI 43542938298 attempt 1](https://github.com/realkarych/kora/actions/runs/43542938298), finished with 37 successful jobs, one cancelled job and five failed dependent gates. [Linux Python 3.15 general-4 job 132287671070](https://github.com/realkarych/kora/actions/runs/43542938298/job/132287671070) had an assigned runner and the annotation `The job has exceeded the maximum execution time of 10m0s`. Its execution/evidence/completion and post steps succeeded, but the job reached its 600-second deadline. The separate 590-second headroom warning is diagnostic; it is not the failed gate. This remains a kora failure and receives no unchanged rerun.

The [failed-lane archive](https://github.com/realkarych/kora/actions/runs/43542938298/artifacts/14093316111), SHA-256 `78547437787f8a2813e22496390ae80a7bfdb1c2eb1e13e1f6ff71d9923f7801`, has a matching receipt and three zero child exits. JUnit records 1,306, 1,547 and 1,556 cases without failures, errors or skips; child wall times were 390.536, 508.299 and 540.612 seconds. These successful contained checks cannot override the cancelled job. The scaffold-evidence job refused `LANES_RESULT: cancelled`; three platform aggregates refused that evidence failure, and CEL lifetime refused the Scaffold failure. No separate failure appears in those logs.

The current table lacks two newly landed Claim snapshot modules: `tests/engine/test_claim_snapshot.py` took 218.104 seconds across 56 cases in child 3, and `tests/observability/test_claim_snapshot_diagnostics.py` took 84.061 seconds across 14 cases in child 2. New admission-wait reason tests contributed 36.411 seconds across 42 cases in child 2. This establishes unrecorded work, not a complete attribution of every timing difference. Added modules independently trigger the prescribed refresh.

## Qualified source and reproducibility

The sole timing source is the latest complete successful [master push 43539882041](https://github.com/realkarych/kora/actions/runs/43539882041), attempt 1, revision `5ac6ab4afb1b200e2e41f7b9cec3f51341566b0c`. GitHub reports a completed successful push to master and all 43 jobs succeeded. Its 19 scaffold archives were downloaded through GitHub's artifact API and each ZIP matched GitHub's SHA-256. The failed newer master run contributes no calibration data.

The existing full verifier ran against the source's original table and original general-lane counts: four on macOS Python 3.11, five on Linux Python 3.11 and four on Linux Python 3.15. It verified receipts, exact run/attempt/revision, file hashes, successful exits, assignments and complete inventory: **three environments, 19 lanes, 56,531 tests**. The generator, both consumers, workflow and original duration table are identical between source `5ac6ab4` and implementation base `4c82de4`.

```bash
uv run --no-sync python -m tests.scaffold_evidence verify <artifacts> 43539882041 1 5ac6ab4afb1b200e2e41f7b9cec3f51341566b0c 5ac6ab4afb1b200e2e41f7b9cec3f51341566b0c '{"macos-15-3.11.17":4,"ubuntu-24.04-3.11.17":5,"ubuntu-24.04-3.15.0":4}' push
uv run --no-sync python -m tests.scaffold_evidence durations <artifacts>
```

A second deterministic encoding through the existing `recorded_table`, `validated_collection` and `table_text` functions matches the committed table bytes, SHA-256 `9f8214f334f1a4a58676e2a46072f8a7a7cef27c2bff470410792df4648af350`. The old table came from `43530883000` at `5dacf3a` and recorded 549 modules; the new table records 555 per environment. Total recorded module times are 2,927.4 seconds on macOS Python 3.11, 4,014.3 on Linux Python 3.11 and 4,269.4 on Linux Python 3.15. Newer admission-wait modules are absent from this successful source and keep the existing environment-mean estimates until a later green master records them.

| Artifact | ID | Archive SHA-256 |
|---|---|---|
| scaffold-tests-macos-15-3.11.17-general-4 | [14092755821](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092755821) | `61ae47644d7f14c865334f8531276a900a99bd8b007e7a42331ffb65142b158d` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-2 | [14092440729](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092440729) | `aeaf73400807e115b19062e29385e3d1173637f89c0dbbb2b51b16f37ed09524` |
| scaffold-tests-macos-15-3.11.17-local-runtime | [14092356426](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092356426) | `bd78dbf84f62251d7b2c7d1260ab23dfc05b4a7a0ada121a2e73f496129af87b` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-1 | [14092226454](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092226454) | `80d7800215aca35f573d4cd77bf607f56884cdfedd95b3905e2ef76256c70cf3` |
| scaffold-tests-macos-15-3.11.17-general-3 | [14092198551](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092198551) | `2878ef9021e950206b2224152403ebf15e31e34ae7f0ec74758e7ec2f98051f4` |
| scaffold-tests-macos-15-3.11.17-general-1 | [14092059006](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092059006) | `de884e1453b58483543cd96fd06f97b25e64d9421f287b63565108c5e6bded0b` |
| scaffold-tests-macos-15-3.11.17-general-2 | [14092053597](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092053597) | `8180919671cf76670b3e9c9698b5dc30e3dcf0c530a02ac726c615c4e0af53b4` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-3 | [14092008267](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14092008267) | `74612819f39eb2b69b52c475992db5dae8681b464ed98d8dc38cd789527e36a3` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-4 | [14091996978](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091996978) | `54e4919deef6e25212d6fc4479621a0dca38f108723926fad2225f1aa33d4f42` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-2 | [14091892075](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091892075) | `972ca8a5cee02ab94648f75a1e922b255a708ffe8d5ae51250c0bf35b476fded` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-5 | [14091797260](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091797260) | `b7fa28040628a73ef6fcddf28187503326be7da6d10d41c3d0d2c3384c5808ad` |
| scaffold-tests-macos-15-3.11.17-storage | [14091681824](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091681824) | `ad78881e31414e24dcd4e10c3d4fc3390fd34b74908deed279c9d19cc122dd66` |
| scaffold-tests-ubuntu-24.04-3.15.0-storage | [14091582754](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091582754) | `a3a65b7df3f4b5d42dcdeb3022fbd102ba31039403de4a3ed2bd3399dedde791` |
| scaffold-tests-ubuntu-24.04-3.11.17-storage | [14091516636](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091516636) | `6ba4dae961c84990f8cc7e63392a6b9aae28bc32d0cee8b066c5c31621de329e` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-1 | [14091502073](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091502073) | `4d806d8a84f60ed2ac43eb844a34f42ae8e9a8d5a86a27e411123c6213881a2f` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-3 | [14091396962](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091396962) | `766d150f84d3a12bb4bb2e250bd51e30dd8543c336f5dde28b97c9370346a12e` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-4 | [14091367494](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091367494) | `113becdbff0268038f9a95a406426b13c52380577a1aa00c6322260be2891001` |
| scaffold-tests-ubuntu-24.04-3.11.17-local-runtime | [14091233192](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091233192) | `1d78c5e3f247b7c2ccfccd112d7f4dfdf4aac4e30456802bffb09c673ab5e540` |
| scaffold-tests-ubuntu-24.04-3.15.0-local-runtime | [14091133916](https://github.com/realkarych/kora/actions/runs/43539882041/artifacts/14091133916) | `2142c0cb29bd318963c399a1efb7bb7690824131a8afb3adfb3747b569952221` |

## Assignment and repetition effects

The existing `general_nodes`, `lane_count` and `assign` functions compare old and new tables against each environment's retained complete general collection. For both plans, the union equals the input, every node appears once, all child estimates are at most 270 seconds and lane counts stay within eight. These deterministic assertions passed.

| Source environment | General tests | Old lanes | New lanes | Old largest estimate | New largest estimate |
|---|---:|---:|---:|---:|---:|
| macOS 15, Python 3.11.17 | 16,837 | 4 | 4 | 250.355 s | 257.224 s |
| Ubuntu 24.04, Python 3.11.17 | 18,260 | 5 | 5 | 265.550 s | 268.212 s |
| Ubuntu 24.04, Python 3.15.0 | 18,260 | 4 | 6 | 260.066 s | 237.701 s |

Current base `4c82de4` adds admission-wait tests after that source. Its fresh local macOS/SQLite collection, obtained with the existing scaffold collection function, has 16,911 general cases; old/new plans both use four lanes, with maxima 251.453/257.224 seconds. The two removed source node IDs are predecessor-step parameter renames in the Claim source migration test; 76 IDs are added across the admission-wait tests and the source downgrade test. No table change removes a test.

The failed master lane's successfully captured, revision-bound Linux Python 3.15 collection contains 18,378 general cases. As an inventory-only diagnostic comparison, its old/new plans use four/six lanes with maxima 261.747/239.314 seconds; set equality, unique coverage and the same deterministic bounds passed. Its timings are not regeneration input, and neither this comparison nor the local macOS collection certifies Linux final-head execution. No Linux collection on a macOS host is substituted for actual Linux evidence.

Changed-test repetition reads the Linux Python 3.11 table too. Its unrecorded-module median changes from 1.407 to 1.410 seconds; estimated ordering and the number of executions fitting the existing window may change. Selection, 300-second window, maximum 40 rounds, failure behavior and permissions remain unchanged. This PR changes no test module, so its repetition selection is empty.

## Verification and trust limits

`uv run --no-sync pytest -q tests/architecture/test_lane_plan.py tests/architecture/test_scaffold_evidence.py tests/architecture/test_changed_test_repetition.py --junitxml=<report>` passed **172 tests in 4.08 seconds**, exit 0, on macOS CPython 3.11.17 with locked dependencies. JUnit SHA-256 is `fca3c64386ce29358cc55148b1e29217540c906fdfd5611c79368a2fc6d5bab7`. Tests ran at `4c82de4` plus the exact generated-table delta, before this prose record. Full Ruff lint/format and strict Pyright passed. Only the existing locked extras/build group were installed; dependency files are unchanged. Independent actual-diff review and final-head checks are recorded in the PR.

TM-12 remains the relevant threat: artifact identity, complete evidence admission and contributor-controlled CI. Estimates change placement, while source trust, complete-inventory refusals, permissions, secrets and required statuses remain unchanged. No threat-model or verification-policy change is introduced.

The latest available [master mutation audit 43412814533](https://github.com/realkarych/kora/actions/runs/43412814533), source `4e586da41651b7cc4516e75b566c534919d4a8fb`, is partial by budget with restored continuation, 113 executed entries and zero reused. Its scopes are pricing/CEL paths, with no applicable calibration finding. It provides no CI-calibration qualification and adds no PR mutation campaign.

The original timeout stays failed. New module estimates and hosted-runner variation limit any prediction; one complete successful final-head CI and scanners remain mandatory, followed by the merge's master run to completion.
