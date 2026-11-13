# Scheduler expiry CI calibration, 2026-11-13

This record accompanies [issue #1444](https://github.com/realkarych/kora/issues/1444) and the routine duration-table refresh specified by [the development procedure](../../docs/development.md) under [issue #1348](https://github.com/realkarych/kora/issues/1348). The planner, test inventory, supported environments, required checks, 270-second estimate target and hard ten-minute job deadlines are unchanged.

## Failed execution

[CI run 43396848871](https://github.com/realkarych/kora/actions/runs/43396848871), attempt 1, tested PR head `65ec04a687f2efbbf09a26340777c44c6917e6fb`. Its Linux Python 3.11 general-1 [job 131791276224](https://github.com/realkarych/kora/actions/runs/43396848871/job/131791276224) was cancelled with the annotation `The job has exceeded the maximum execution time of 10m0s`. The test step ran from 09:00:18 to 09:10:09 UTC. Two children completed with 1,547 and 2,094 passing tests in 403.79 and 409.28 seconds. The third child did not finish or produce JUnit evidence. Its buffered log does not identify a hung test.

This is a kora failure under AGENTS.md, not an infrastructure rerun candidate. The incomplete execution supplies no passing lane result. The repair updates calibration inputs before a new complete final-revision execution; it does not rerun unchanged content.

## Refresh source and scope

The previous table came from master run `43132494211`, revision `90968d663e591b24ac7feaed4cc36d4c9d3e4388`. It contained 482 general modules per environment. The updated table comes exclusively from successful master push [43391583848](https://github.com/realkarych/kora/actions/runs/43391583848), attempt 1, revision `c600274a8a6673f94316399280aad3dbc4d8c28b`, completed at 08:43:48 UTC on 2026-11-13. GitHub records the event as `push` and conclusion as `success`.

The source has 506 general modules per environment. In particular, the earlier table lacks `tests/engine/test_run_promotion.py`, added by the previous scheduler delivery. Its 60 tests receive an old Linux Python 3.11 default estimate of 8.16 seconds (60 tests at 136 milliseconds after rounding), whereas this source records 95.15 seconds. The failed lane placed promotion and the new expiry tests in the same child. Added modules independently trigger the approved refresh; this change does not select a single unusually slow run merely to obtain more lanes.

The nine Linux Python 3.11 general children in the source ran for 223.99–392.87 seconds. These are hosted-runner observations, not a timing guarantee or attribution of the entire failed child's delay to one module. New expiry modules remain unrecorded and use the existing environment-mean estimate until a green master execution contains them.

The existing command generates the complete table from the source artifacts:

```bash
gh run download 43391583848 --pattern 'scaffold-tests-*' --dir <artifacts>
uv run --no-sync python -m tests.scaffold_evidence durations <artifacts>
```

The generator checks receipt identities and successful exit files. GitHub's complete successful workflow supplies the source-run qualification. Re-generating from those artifacts produces exactly the committed bytes, SHA-256 `4dd36c0d479e67f1e71683f72424574d4a19f5ab2359a62458450ee6b03011a6`. The resulting recorded totals are 2,513.8 seconds on macOS Python 3.11, 2,757.0 seconds on Linux Python 3.11 and 2,917.5 seconds on Linux Python 3.15.

## Partition comparison

The question is whether the existing algorithm incorporates the newly available module durations while retaining the exact collected test set and its deterministic estimate bound. Applying both tables to the PR's preserved Linux and macOS collections gives:

| Environment | General tests | Previous lanes | Updated lanes | Previous largest estimate | Updated largest estimate |
|---|---:|---:|---:|---:|---:|
| macOS 15, Python 3.11.17 | 16,216 | 4 | 4 | 228.28 s | 210.70 s |
| Ubuntu 24.04, Python 3.11.17 | 17,273 | 3 | 4 | 262.26 s | 231.55 s |
| Ubuntu 24.04, Python 3.15.0 | 17,273 | 4 | 4 | 210.31 s | 244.91 s |

The Linux collection is the shared planning inventory for both Python versions. In every comparison, the union of children equals that collection exactly, without duplicates, and each child's estimate remains at most 270 seconds. Neither the number of tests nor an elapsed-time estimate proves that a job will finish within its deadline. The added Linux lane does not change macOS concurrency or its lane count.

The changed-test repetition job also consumes the Linux Python 3.11 table. Refreshing it changes the initial execution estimates, duration-based ordering and which repetitions fit the existing window. Its unknown-module median changes from 1.214 to 1.0475 seconds. The refresh does not guarantee a number of repetitions. The selected module set, 300-second window, maximum 40 rounds, failure behavior and permissions are unchanged. This is an existing calibration consumer, not a new repetition method or gate.

The existing `tests/architecture/test_lane_plan.py` and `tests/architecture/test_scaffold_evidence.py` checks pass: 112 tests in 1.17 seconds. They cover the deterministic partition, unknown-module estimates, table representation and evidence refusal. The shared consumer's `tests/architecture/test_changed_test_repetition.py` also passes: 47 tests in 5.53 seconds. Final-revision CI remains required and is recorded in the pull request.

## Independent delivery under issue #1464

The same table is delivered separately from scheduler expiry so dependent repairs can use it. Base `81846e7d98976f18db1f30e3049198a1fa3d883d` still contains the old table. Diagnostic PR #1477 at `2eee65f491125e96303cb227a285943a021451b4` encountered another hard timeout in [run 43415700291](https://github.com/realkarych/kora/actions/runs/43415700291), Linux Python 3.11 general-2 [job 131854994145](https://github.com/realkarych/kora/actions/runs/43415700291/job/131854994145). The job ran on its assigned runner from 11:26:29 to 11:36:39 UTC; the test step started at 11:26:56.983 and was cancelled at 11:36:31.756 UTC; its annotation states that the maximum ten-minute execution time was exceeded. A child completed 1,962 tests in 394.04 seconds, but the lane did not complete. This remains a kora failure, without an unchanged rerun or a claim to identify a particular hung test.

The [extraction plan](https://github.com/realkarych/kora/issues/1464#issuecomment-6851801279) changes only the generated duration table and this record. Its table bytes, source run, generator, planner, repetition consumer, workflows, dependencies and verification method are unchanged from the reviewed expiry revision. The current collection differs, so the earlier table of partition results remains historical evidence only.

The current comparison uses the complete collections from master run `43415310983`, revision `81846e7d98976f18db1f30e3049198a1fa3d883d`: Linux general-2 artifact `14032107034` and macOS general-2 artifact `14032078208`. These artifacts supply inventories, not a new duration-table source or a claim that the entire master run had completed when they were read. The ordinary `validated_collection`, `general_nodes`, `lane_count` and `assign` functions compare both tables. The union and count of assigned tests equal each input exactly, and every child estimate remains at most 270 seconds.

| Environment | General tests | Previous lanes | Updated lanes | Previous largest estimate | Updated largest estimate |
|---|---:|---:|---:|---:|---:|
| macOS 15, Python 3.11.17 | 16,202 | 4 | 4 | 228.084 s | 210.522 s |
| Ubuntu 24.04, Python 3.11.17 | 17,251 | 3 | 4 | 261.924 s | 231.253 s |
| Ubuntu 24.04, Python 3.15.0 | 17,251 | 4 | 4 | 210.047 s | 244.595 s |

New manual-start acceptance modules are absent from the source calibration and use the existing unknown-module estimates. The separate PR changes no test module, so Changed test repetition selects zero modules under its unchanged selection rule; complete current-inventory coverage remains the responsibility of ordinary final-head scaffold CI. The existing planner, evidence and repetition regression command passes 159 tests in 3.20 seconds on this base with the copied table. Independent correctness, security and performance conclusions and complete final-head CI are recorded in the PR; the original failed runs are not reclassified.
