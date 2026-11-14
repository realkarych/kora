# Worker and scheduler CI calibration, 2026-11-14

This record supports [issue #1528](https://github.com/realkarych/kora/issues/1528) and the existing [duration refresh procedure](../../docs/development.md#ci-and-contributions). Only generated duration data changes. The planner, three children, 270-second estimate target, eight-lane maximum, hard ten-minute job deadline, tests, supported environments, permissions and required statuses are unchanged.

## Failure and maintenance trigger

PR #1516 head `caf82e2a18f5dfd4356e9a1f286742dc23f29b45`, [CI 43531346700 attempt 1](https://github.com/realkarych/kora/actions/runs/43531346700), timed out in [Linux Python 3.11 general-3 job 132250234023](https://github.com/realkarych/kora/actions/runs/43531346700/job/132250234023). GitHub assigned a runner and recorded `The job has exceeded the maximum execution time of 10m0s`. The three children returned zero and produced JUnit with no failures, errors or skips: 1,771 tests in 432.589 seconds, 1,088 in 545.280 seconds and 1,645 in 328.265 seconds. The complete-lane gate nevertheless refused the cancelled workflow. Dependent scaffold and CEL aggregation jobs inherited that refusal. This remains a kora failure and received no unchanged rerun.

The [failed-lane artifact](https://github.com/realkarych/kora/actions/runs/43531346700/artifacts/14088500742), SHA-256 `3726fe3e259f2624dfa06b663f05b3dcd47d99dadce70f38aeab0bb26d9988fe`, establishes substantial unrecorded work. The old table lacks the newly added run-settlement module, which took 130.52 seconds in child 2, and start acceptance, which took 78.83 seconds in child 1. Each child was planned at 244.9 seconds. Other recorded modules also ran above their estimates; this does not attribute every delay to missing entries or guarantee future runner time.

The previous table came from run `43391583848` at `c600274a8a6673f94316399280aad3dbc4d8c28b` and recorded 506 modules per environment. The new source includes 43 additional modules, including capacity, expiry, settlement, Claim material and manual-start acceptance. Added modules independently trigger the prescribed refresh; selecting an unusually slow run is not its justification.

## Qualified source and reproducibility

The sole source is successful [master push 43530883000](https://github.com/realkarych/kora/actions/runs/43530883000), attempt 1, revision `5dacf3a5645a505819f0b9914e7c288f2091a02a`. GitHub records a completed successful push and 42 successful jobs. All 18 scaffold archives were downloaded through the GitHub connector and verified against GitHub's SHA-256 digests. Local GitHub CLI requests encountered a TLS timeout; the connector retrieved the same immutable artifact IDs without changing their admission.

The existing full verifier, run before regeneration against the original table and four general lanes per environment, returned three environments, 18 lanes and 56,052 tests. It checked receipt identity, hashes, successful exits, assignments and the exact complete union. Generator receipt checks alone are not the evidence of a complete successful workflow.

```bash
uv run --no-sync python -m tests.scaffold_evidence verify <artifacts> 43530883000 1 5dacf3a5645a505819f0b9914e7c288f2091a02a 5dacf3a5645a505819f0b9914e7c288f2091a02a '{"macos-15-3.11.17":4,"ubuntu-24.04-3.11.17":4,"ubuntu-24.04-3.15.0":4}' push
uv run --no-sync python -m tests.scaffold_evidence durations <artifacts>
```

Regenerating the table with the existing `recorded_table`, `validated_collection` and `table_text` functions yields exactly the committed bytes, SHA-256 `4355efcbbfc78304ef9669c6bad240f00346ee43446fe499cd3b5434dc93d006`. It records 549 modules per environment, totaling 2,977.1 seconds on macOS Python 3.11, 3,934.7 seconds on Linux Python 3.11 and 3,079.1 seconds on Linux Python 3.15.

| Artifact | ID | Archive SHA-256 |
|---|---|---|
| scaffold-tests-macos-15-3.11.17-general-4 | [14089170328](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14089170328) | `b5386127180214313c628abaef6a6a79096057de3c3287de2fce15867596ade2` |
| scaffold-tests-macos-15-3.11.17-general-3 | [14088845559](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14088845559) | `c0a5006b304db7ffe160375e07fa132420adf914ba93b23a87e040e18f94580c` |
| scaffold-tests-macos-15-3.11.17-general-2 | [14088386002](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14088386002) | `80c54e10d1eaee6b717a1e58153f8a3dd83cc737a6069511585fa5dcd5522121` |
| scaffold-tests-macos-15-3.11.17-general-1 | [14088296182](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14088296182) | `48c0fa6a47f900e322da82f0096069ab0f8fb8a7c7ed4e7db1a35e3e77802074` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-4 | [14088230576](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14088230576) | `187cd3bf51b31de5d58fd0f59e606706a038b2a8daa3e2e95504e639af5fa1e1` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-1 | [14087886099](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087886099) | `5856956aa7ade1ead3822dc0d2005f0977d1815f21522efcb9638f24ef71d702` |
| scaffold-tests-ubuntu-24.04-3.11.17-storage | [14087712174](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087712174) | `d6819c1e171f452170e76247c3010aeb9adbe320e285347592471ddc101e3ddd` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-3 | [14087667256](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087667256) | `8ea6dd9e4bfa27918eed787e1329532514b77c9492e4ea102f49d16f2731e62c` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-1 | [14087622394](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087622394) | `aaf8725492718d99b1f59cdad3ec8d0c7845dba3605c9da1403b93962838c611` |
| scaffold-tests-ubuntu-24.04-3.15.0-storage | [14087452682](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087452682) | `e8de6e6d0e9d08766a542cd38cada2b2d6655b4612051a2b8bf2525d0ef77882` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-4 | [14087407713](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087407713) | `48b3533ba44c1ec9a3cbe76cebef58ae8e6c098d3d397fd5abf8e7c3e891b9ea` |
| scaffold-tests-ubuntu-24.04-3.11.17-local-runtime | [14087332766](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087332766) | `1213f322de63d0aedd0550e75f4884654f7e2e3fdfb21b10cf97fb056dd1b848` |
| scaffold-tests-macos-15-3.11.17-storage | [14087263467](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087263467) | `c96129f035234fcdacb7fe7b92d920ac719f62722a949e70278906ba7582eb5f` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-2 | [14087143746](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087143746) | `b2a1f1aeaa1062453affe44593c46e651bda88940f10cda13baf598615202abf` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-3 | [14087004096](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14087004096) | `a52850aa397050c456e41ece1879afa3990134ef2466a617b1d678eacd231d91` |
| scaffold-tests-macos-15-3.11.17-local-runtime | [14086949460](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14086949460) | `27e5a7c5348df641468eaada9959a5e64a76f168d6c68811ea0adda13fdda9fd` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-2 | [14086894804](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14086894804) | `47d211559f084899f0112a3c08fb0bcbd671f131ccbe8cde0f568ad4e01bf709` |
| scaffold-tests-ubuntu-24.04-3.15.0-local-runtime | [14086634631](https://github.com/realkarych/kora/actions/runs/43530883000/artifacts/14086634631) | `63a78a9bc0bdd7ff6225e0c08329dfef9bac89873db6f95e65521fc9b8b4dd67` |

## Assignment and consumer effects

The existing `general_nodes`, `lane_count` and `assign` functions compare old and new tables against each environment's retained complete collection. For each plan, the union equals the input exactly, the sum of child counts equals its size, and every child estimate stays at most 270 seconds. Source and refresh have identical test inputs at the same base revision. A fresh local collection using the existing scaffold collection function, macOS selection and SQLite exactly matches the 16,728 general tests in the retained macOS source. A diagnostic Linux-selection collection on the macOS host differs by platform-dependent process and immutable-file cases: 18,069 versus 18,075. That failed equality probe is not Linux evidence; the actual Linux source inventories supply the Linux comparison, and final CI validates them on Linux.

| Environment | General tests | Old lanes | New lanes | Old largest estimate | New largest estimate |
|---|---:|---:|---:|---:|---:|
| macOS 15, Python 3.11.17 | 16,728 | 4 | 4 | 218.555 s | 248.738 s |
| Ubuntu 24.04, Python 3.11.17 | 18,075 | 4 | 5 | 243.805 s | 262.861 s |
| Ubuntu 24.04, Python 3.15.0 | 18,075 | 4 | 4 | 257.833 s | 257.430 s |

The source's actual child wall times range from 188.747–328.729 seconds on macOS, 209.615–451.545 on Linux Python 3.11 and 216.296–319.585 on Linux Python 3.15. These are diagnostics, not new bounds or proof that a future job finishes before its deadline. New modules on unmerged feature branches still receive the existing environment-mean estimates until a successful master run records them.

Changed-test repetition also reads the Linux Python 3.11 table. Its unrecorded-module median changes from 1.0475 to 1.407 seconds; initial estimates, ordering and the number of executions fitting its existing 300-second window can change. Its selection, maximum 40 rounds, failure behavior and permissions do not change. This refresh modifies no test module, so its own PR selects no changed modules for repetition.

## Verification and trust limits

The focused command `uv run --no-sync pytest -q tests/architecture/test_lane_plan.py tests/architecture/test_scaffold_evidence.py tests/architecture/test_changed_test_repetition.py` passed 172 tests in 3.75 seconds on macOS CPython 3.11.17 with locked dependencies. JUnit SHA-256: `fc9125954bb27eb489ba580675c0ccd284e3cecd8bee474d759e1d348ff884b5`. Full Ruff lint and format checks passed. Initial strict typing found missing local build dependencies; installing the existing locked build group is the environment repair, with no dependency change. Strict typing passed after that environment repair. The research directory is excluded by the repository Markdown ignore file; an explicit stdin lint found a trailing blank line, which was corrected. Independent actual-diff reviews and final-head checks are recorded in the pull request.

The relevant threat is TM-12: contributor changes to CI and evidence integrity. The existing threat-model entry remains accurate: the refresh changes estimates and placement data, while source-revision trust, exact-union evidence admission, secrets, permissions and required gates remain unchanged. No threat-model or verification-policy change is proposed.

Available [master mutation audit 43412814533](https://github.com/realkarych/kora/actions/runs/43412814533), source `4e586da41651b7cc4516e75b566c534919d4a8fb`, reports partial execution at its budget with restored continuation, 113 executed entries and no reused results. Its declared scopes are pricing and CEL paths; it supplies no applicable CI-calibration finding and no CI coverage claim. No PR mutation campaign is added.

Final-head CI, Linux/macOS coverage and all required scanners remain mandatory. The old failure is not reclassified by the successful source or local checks.
