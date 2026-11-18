# Scheduler admission CI calibration, 2026-11-14

This record supports the duration refresh prepared in [issue #1566](https://github.com/realkarych/kora/issues/1566) and consolidated into [issue #1562](https://github.com/realkarych/kora/issues/1562), under the existing [duration refresh procedure](../../docs/development.md#ci-and-contributions). The calibration changes only generated timing estimates; the combined repair also removes structural code hashing from the admission test observer. The planner, three children per lane, deterministic 270-second planning bound and its single-test exception, eight-lane maximum, ten-minute job deadline, required tests, environments, permissions and statuses remain unchanged. Recorded time and planning estimates are diagnostics; complete evidence admission, unique inventory and deterministic bounds remain blocking.

## Maintenance trigger and retained failure

[PR #1565 CI 43635121283 attempt 1](https://github.com/realkarych/kora/actions/runs/43635121283), head `790fce31604e1b31b5e460d35acbef9690e126cb`, completed with 33 successful jobs, one cancelled lane, five dependent failures and two conditional skips. [Linux Python 3.11 general-3 job 132570423910](https://github.com/realkarych/kora/actions/runs/43635121283/job/132570423910) had an assigned runner and the annotation `The job has exceeded the maximum execution time of 10m0s`. Its execution step was cancelled. The first child stopped at 63% during dependency-settlement cases and produced no final XML or receipt. The other children completed 1,230 and 962 cases without failures, errors or skips. The partial first-child log contains no failed assertion, but cannot establish completion of its remaining tests.

The shared evidence job refused the missing first-child report and lane receipt. Three Scaffold aggregates and CEL lifetime refused their prerequisite results. This is a kora hard-timeout failure, not an infrastructure rerun candidate. The workflow took 11m56s; that separate feedback-target miss does not explain or replace the failed hard job deadline. The [failed lane artifact](https://github.com/realkarych/kora/actions/runs/43635121283/artifacts/14130342087) has GitHub archive SHA-256 `3c0ee21c992d70448ee99f41f068fbc27d10fa58772dfe049a656dc5dc05092f`. No unchanged-content rerun was made.

The previous table came from master run `43539882041`, revision `5ac6ab4afb1b200e2e41f7b9cec3f51341566b0c`, and recorded 555 modules per environment. It lacks the later dependency-settlement and admission-preparation modules. Added modules independently trigger the prescribed refresh. The missing estimates do not fully attribute every elapsed-time difference. The original admission-preparation refusal is documented in #1562; both 24-case Linux preparation modules passed in the failed PR workflow, without making that workflow successful.

The separate calibration [PR #1567 run 43637434476 attempt 1](https://github.com/realkarych/kora/actions/runs/43637434476), head `e71bc22e4956aa97a26ea4bdf76080b41c1f973c`, completed all test lanes within their hard deadlines but failed the old observer's SQLite/distinct preparation case with `AdmissionPreparationRejected`. [Linux Python 3.11 general-3 job 132577433744](https://github.com/realkarych/kora/actions/runs/43637434476/job/132577433744) failed a test assertion; five dependent evidence/aggregate gates also failed. The workflow finished in 11m16s with 35 successes, six failures and two conditional skips. The [lane artifact](https://github.com/realkarych/kora/actions/runs/43637434476/artifacts/14130627493) has GitHub archive SHA-256 `22f636bfdf314985816f0e60406d191518d087395e26dfbeb8cb66197136a81d`. This is another retained kora failure, not an infrastructure rerun candidate or proof of the refusal's internal cause. The exact two reviewed repairs are therefore delivered together in PR #1565; #1567 is superseded. Neither failed execution is repeated unchanged.

## Qualified source

The sole regeneration source is successful [master push 43627734292](https://github.com/realkarych/kora/actions/runs/43627734292), attempt 1, revision `a8174fe57f967a55336a95cbb288dcb0e4f14668`. GitHub reports all 46 jobs successful. All 22 scaffold artifacts were acquired from that run through the authenticated GitHub CLI. The existing full verifier admitted their receipts, run/attempt/revisions, file hashes, successful exits, exact assignments and complete inventories: **three environments, 22 lanes and 58,299 tests**, exit 0.

The generator, verifier, planner, repetition consumer, CI workflows and dependency manifests are identical between the source and implementation base `784601fbf43e834af41c7c14bd3d66cbaed4c84a`. The intervening parameter-schema test changes add collection and continuation assertions without changing cases or parametrization; their source timings remain historical estimates, not measurements of the newer implementation. The failed PR contributes inventory comparison only, never regeneration timings.

```bash
uv run --no-sync python -m tests.scaffold_evidence verify <artifacts> 43627734292 1 a8174fe57f967a55336a95cbb288dcb0e4f14668 a8174fe57f967a55336a95cbb288dcb0e4f14668 '{"macos-15-3.11.17":4,"ubuntu-24.04-3.11.17":6,"ubuntu-24.04-3.15.0":6}' push
uv run --no-sync python -m tests.scaffold_evidence durations <artifacts>
```

The source is not a close fit to its old plan: 18 of 48 children exceed their estimate by more than 25%; recorded/planned ratios span 0.763–1.835. Linux Python 3.11 general-3 child-1 records 425.3 seconds against 231.8 planned. The procedure prefers a close-fit source but makes added modules and master notices independent refresh triggers. Hosted-runner variation remains a limitation. No faster source was selected to obtain a favorable result.

The generated table records 592 modules per environment, with total module times of 3,024.5 seconds on macOS Python 3.11, 5,337.7 on Linux Python 3.11 and 5,161.7 on Linux Python 3.15. A second deterministic encoding through the existing `recorded_table`, `validated_collection` and `table_text` functions matches its bytes: SHA-256 `64d3a47e79af078c12c035722677f40cd757157f12c8d77d069d5096b26e5853`.

For example, the source records Linux Python 3.11 preparation at 115.167 seconds across 24 cases and dependency settlement at 74.511 seconds across 26 cases; both previously used unknown-module estimates. The remaining new modules receive their actual source measurements too. No product limit is inferred from these module totals.

## Planner and repetition effects

The existing `general_nodes`, `lane_count` and `assign` functions compare old/new tables against every retained source environment and the failed PR's complete Linux Python 3.11 collection. All comparisons preserve the exact input union and unique test ownership. Every child estimate is at most 270 seconds and every environment uses at most eight lanes; none needs the single-test exception.

| Inventory | General tests | Old lanes | New lanes | Old largest estimate | New largest estimate |
|---|---:|---:|---:|---:|---:|
| Source macOS 15, Python 3.11.17 | 17,195 | 4 | 4 | 257.224 s | 252.760 s |
| Source Ubuntu 24.04, Python 3.11.17 | 18,877 | 6 | 7 | 231.821 s | 254.643 s |
| Source Ubuntu 24.04, Python 3.15.0 | 18,877 | 6 | 7 | 246.370 s | 246.287 s |
| Failed PR Ubuntu 24.04, Python 3.11.17, inventory only | 18,877 | 6 | 7 | 231.821 s | 254.643 s |

This uses the existing automatic lane-count policy; it does not change configured limits or promise shorter hosted execution. PRs retain their approved macOS placement, while master still executes its general macOS lanes.

Changed-test repetition uses Linux Python 3.11 module totals rather than per-test averages. Its unknown-module median changes from 1.410 to 1.610 seconds. Across the union of recorded module paths, 583 positions differ when treated as modified modules and ordered through the existing `ordered` function. No module crosses the existing 300-second initial-estimate threshold in either direction, including previously unknown modules evaluated at the old median. Actual executions fitting the window may still change. Selection, added-first ordering, the 300-second window, maximum 40 rounds, failure behavior and hard job deadline are unchanged. This change modifies no test module, so its repetition selection is empty.

## Verification and limits

On macOS arm64/Python 3.11.17 with the existing locked build group and extras, `uv run --no-sync pytest -q tests/architecture/test_lane_plan.py tests/architecture/test_scaffold_evidence.py tests/architecture/test_changed_test_repetition.py` passed **172 tests in 4.01 seconds**, exit 0. JUnit SHA-256: `ea3c80d4ba5ef9d340ce3b4fdb9bbd3e9b14651ff113eb8fe0f8fb68fd85e0d0`. These tests exercise real planner, evidence and repetition implementations without metadata-database access; no new database test is needed. Full Ruff lint/format and strict Pyright passed. Tests ran at the implementation base plus the exact generated-table delta; this record adds no executable change. Independent actual-result reviews and final-head CI are recorded in the PR.

TM-12 remains the relevant threat: authenticated artifact provenance, complete evidence admission and contributor-controlled CI. The table affects planning and repetition estimates but grants no execution authority and cannot waive inventory, integrity, deadline or required-status checks. No threat-model, workflow or verification-policy change is introduced. Existing timing notices remain diagnostics; runtime logging is unchanged.

The latest inspected master mutation audit, [43584820056](https://github.com/realkarych/kora/actions/runs/43584820056), attempt 1 at `d623a105c17ac682e8017a469fc8c6535990ec28`, covers partial pricing/CEL scope with no applicable calibration finding. Its report digest is `a16a15546e915a1196b860912f60d1414dca7694179edf884af27d441d80b392`. It adds no PR mutation campaign or merge hold.

The historical timeout and preparation refusal remain failed. Source workload changes, the combined observer repair and shared-runner variation limit prediction. One complete successful CI and scanners on the combined PR #1565 revision remain required, followed by the merge's master CI to completion. Consolidation changes neither the generated bytes nor the original observations and does not substitute passing component results for a successful complete workflow.

## Bounded refusal diagnostics, 2026-11-15

The combined [CI run 43638568567](https://github.com/realkarych/kora/actions/runs/43638568567), attempt 1 at `f924662094c2938087fcb8619df714cc894a62d6`, again failed the distinct PostgreSQL preparation case on Linux Python 3.11 and 3.15. Both reports expose only `AdmissionPreparationRejected`; neither the testcase duration nor the prior observer improvement establishes its internal cause. The workflow finished in 13m32s with 34 successes, seven failures and two conditional skips. The two direct test failures and five dependent gate refusals remain kora failures. No unchanged-content rerun was made.

After unsuccessful repairs, the owner approved [one bounded diagnostic step](https://github.com/realkarych/kora/issues/1562#issuecomment-6884048709). The existing test observer adds a note to the same preparation exception only after restoring the previous profiler, then re-raises it. Its callback, five code-identity targets, call list, assertions, deadline and production implementation remain unchanged. The note contains elapsed monotonic seconds in scientific notation, the entry count, at most eight existing fixed function names and the owned-unit count after unwind. The note stays below 1 KiB: the names come from five loaded functions, the two counts describe live list entries and owned operations, and the elapsed representation has fixed precision. It reads no source payload, exception message, cause, context, local variable, command or credential.

Function entry does not prove completion; elapsed time does not identify the original exception or establish a timing guarantee. The unit counter does not independently prove native connection closure. Existing ledger and lifecycle regressions retain that obligation. No new production event, observation target, database query, retry, numerical acceptance method or CI gate is introduced. Further cause repair requires a supported proposal and owner decision.

A single local diagnostic-branch check per real database used the existing short work allowance through ordinary argument injection. SQLite and PostgreSQL both preserved the original exception identity and restored the prior profiler; the note reported zero entries and owned units. A separate connection ledger confirmed the warmed connection closed after database shutdown. This early-refusal check does not exercise or explain the historical late preparation failure. The script's SHA-256 is `be7744a541cda685f120dc61faf62a7e7db2bd0b730ca3770ffc38e6789131cf`; its two-line result is `c9061f56a4027c865797de9f3a2290a31e1d4f0c121d3b94f245bd1d6dfdb85b`. Focused regressions, independent actual-result reviews and the one approved ordinary CI execution are recorded in PR #1565.

## Source artifact identities

The archive digests below are GitHub's artifact metadata. The full verifier checked the extracted files against their admitted receipts; these rows do not claim a separate comparison of downloaded ZIP bytes.

| Artifact | ID | GitHub archive SHA-256 |
|---|---|---|
| scaffold-tests-macos-15-3.11.17-general-1 | [14127807733](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127807733) | `8c1a09f7f807d0931cbe54f1a9630273e53493d3550ded3605445590572259a7` |
| scaffold-tests-macos-15-3.11.17-general-2 | [14127437264](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127437264) | `a3e2460b7b07b8f1ddabf966f90d051afceb6e25b2e72db836098b479f12211a` |
| scaffold-tests-macos-15-3.11.17-general-3 | [14128032633](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14128032633) | `733a4cf7c707a6c299c2c1469c81b28c5fb21b5f9c9a72ff0e5f406ea94e85db` |
| scaffold-tests-macos-15-3.11.17-general-4 | [14128870397](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14128870397) | `b154f7ddc318b8b168cf20b16abb97ac6564a7b62baa0181045c18fd61cf40db` |
| scaffold-tests-macos-15-3.11.17-local-runtime | [14127801098](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127801098) | `d97caf5df7b5dd923a312903c9896da8100c85d72a9f09a6bde6ed016e1b75e8` |
| scaffold-tests-macos-15-3.11.17-storage | [14127870180](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127870180) | `a439943230f142e70be6f1cbba24120b1ef8173c5aa64d6670eecd7d35c9d168` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-1 | [14127520918](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127520918) | `b9df0df0b53d129d4dbd35096d6cc85d0abcb8df3fd4efcf2f7e09785570ae24` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-2 | [14127173284](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127173284) | `37a4482f86fde2f1c3cafd40b7c056825011ea7b9305d0966c80e7340dd98e2b` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-3 | [14127308270](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127308270) | `4caf86d2c6a92aacc50f7d18c8f0baccc5a496543fb80f4ebc4c263de981381e` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-4 | [14127541496](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127541496) | `248deb131b472548c89bb6db1e3d578245dd3c45d83af84353824310c6afcf9a` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-5 | [14127531419](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127531419) | `9fe5b8b127fb7dad6b857e749a6ed6b06bba65de09821c0dfdcb14012e2cd2d9` |
| scaffold-tests-ubuntu-24.04-3.11.17-general-6 | [14127631231](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127631231) | `41427d613a4a8dcf8929f3e8de7d3c16f6faf191105020a4bfa3504e72a71b14` |
| scaffold-tests-ubuntu-24.04-3.11.17-local-runtime | [14127466428](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127466428) | `770b53083796a4775ec2825c0bdcb01eecfdc48aa500092042974afdd4616231` |
| scaffold-tests-ubuntu-24.04-3.11.17-storage | [14126454606](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14126454606) | `4aea9cc553d3f3ab86f66c89dbfddde4392b270fee83ee3a30c6c25bee6de171` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-1 | [14128010404](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14128010404) | `a0a712c7aa65ab42e924f79e8c7f357f2e16ad5e80a672fbe707e327b1a88e88` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-2 | [14127801340](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127801340) | `47449f6d6925a4c817ce11b7e1f5792f5c389f43587df03e2ab267aed0ad89fc` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-3 | [14127891149](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127891149) | `0232fddadd128e5040784148f17875cdb3dc27f3ba5b5e4ec60c7b6a65f339c1` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-4 | [14127561120](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127561120) | `95feb85076540263cc518f867ffbc3d6756c08f92df8785ba421de35bffc583c` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-5 | [14127945235](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127945235) | `48e272859ee2d30d4072972cf5c997a5b450ac754265df81eea60f4d3cf0b2ef` |
| scaffold-tests-ubuntu-24.04-3.15.0-general-6 | [14128065159](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14128065159) | `ad8e7a51246feac51de06971e7a90e3f0d1a8b9fc5b3d1d95494e05d77069654` |
| scaffold-tests-ubuntu-24.04-3.15.0-local-runtime | [14127636075](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127636075) | `4d196869bc7d4f5e1fa8372c0cf951e7204b713166bab24356671a482b54e0c7` |
| scaffold-tests-ubuntu-24.04-3.15.0-storage | [14127039910](https://github.com/realkarych/kora/actions/runs/43627734292/artifacts/14127039910) | `f40f39172b5b23c95ee4c982e682a970e80746e2d163c4e90c9d3c30cb8b5189` |
