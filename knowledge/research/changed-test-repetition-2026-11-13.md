# Changed test repetition, 2026-11-13

Checked on 2026-11-12 and 2026-11-13 for [issue #1440](https://github.com/realkarych/kora/issues/1440), part of [issue #1347](https://github.com/realkarych/kora/issues/1347). The numbers are observations of hosted runners and of one local Linux container. They are not a portable benchmark and set no numerical gate.

## Owner decision

On 2026-11-13 the project owner decided that every pull request CI execution repeats the test modules the pull request adds or changes, and that a failed repetition blocks the merge. This is one named exception to the [bounded MVP verification](bounded-mvp-verification-2026-10-29.md) rule, which removed repeated test-file campaigns as merge requirements. Campaigns of whole pipelines or of fixed files stay removed.

## Why a single execution was not enough

Pull request #1417 added `tests/plugins/runtime/local/test_command_capture.py` together with the local command capture. It passed its one complete CI execution ([run 43236093641](https://github.com/realkarych/kora/actions/runs/43236093641)) and merged as `76d651c` on 2026-11-12 at 14:51 UTC. The module contained two races:

- a test helper published its process id in two steps, so the test could read an empty file ([issue #1423](https://github.com/realkarych/kora/issues/1423), fixed by #1424);
- the product skipped the capture seal when a command stopped before its capture was attached ([issue #1429](https://github.com/realkarych/kora/issues/1429), fixed by #1439 as `bf5f567`).

Between that merge and 21:30 UTC, the Linux `local-runtime` lanes, which run this module in a single pytest process with no other test process on the runner, completed 56 executions that reached their tests. Eight of them failed in this module:

| Run | Event | Failed test | Cause |
|---|---|---|---|
| 43241605896 | master push | `test_command_capture_without_eof_is_truncated` | #1423 |
| 43266609193 | master push | `test_command_capture_without_eof_is_truncated` | #1423 |
| 43276357117 | pull request | `test_command_capture_releases_resources[success]` | #1429 |
| 43296108733 | master push | `test_command_capture_seal_is_bounded[seal-write]`, `[shutdown-sync]` | #1429 |
| 43300125353 | pull request | `test_command_capture_never_delays_stop` | #1429 |
| 43303852555 | master push | `test_command_capture_seal_is_bounded[shutdown-write]` | #1429 |
| 43305283075 | pull request | `test_command_capture_releases_resources[failure]` | #1429 |
| 43307844136 | master push | `test_command_capture_seal_is_bounded[shutdown-sync]` | #1429 |

That is about one failure in seven single-process executions on a hosted Linux runner. A check that executes the changed module 20 times at that rate fails with a probability of about 95%.

## Measurements in a local container

Environment: Docker on an arm64 macOS host, image `ghcr.io/astral-sh/uv:0.13.1-python3.11-trixie-slim`, 4 CPUs, a non-root user, SQLite only, without the CI host fixtures, so the one `host_fixture` test of the module is deselected. Each execution is `uv run --no-sync pytest tests/plugins/runtime/local/test_command_capture.py -q` in a fresh process.

| Revision | Arrangement | Failed executions |
|---|---|---|
| master `28fcfb7` (with #1424, without #1439) | 4 concurrent loops of 40 executions | 14 of 160 |
| #1439 head `d03adbd` | 4 concurrent loops of 40 executions | 0 of 160 |
| master `28fcfb7` | one execution at a time, no other load | 0 of 80 |
| master `28fcfb7` | one execution at a time beside 3 CPU-bound processes | 0 of 79 |

On this faster machine the #1429 race appears only when several copies of the module run at once. CPU-bound background processes did not provoke it. On the hosted runners it appeared without any added load.

## Why executions are sequential

The approved plan first proposed four concurrent copies of the selection. The implementation executes one fresh process at a time instead, for two reasons:

- The lanes run different tests at once, never two copies of one test. Nothing establishes that a test is safe beside a copy of itself: storage pressure tests, for example, attach loop devices and mount filesystems on the runner. Copies of one module running at once could fail for reasons unrelated to the change, and a blocking check must not add failures of its own.
- The hosted-runner history above shows the motivating races in single-process executions, and the container measurement shows that synthetic CPU load adds nothing.

General lanes run three test processes at once, so a general-lane module sees less contention in this job than in its lane. A race that needs that contention can pass the job. See the limits below.

## The job

`Changed test repetition` in `.github/workflows/ci.yml`, implemented by `tests/changed_test_repetition.py`:

- **Selection.** `tests/**/test_*.py` files that the pull request's merge commit adds, modifies or renames relative to its first parent, without the modules the daily workflow runs on Linux and without modules whose tests the repository's default selection, `not measurement and not host_fixture`, leaves out entirely. A module that cannot be collected, a successful collection whose listing is empty or names a file other than the changed modules, and a changed test module path with characters outside letters, digits, `_`, `.`, `/` and `-` each fail the job.
- **Execution.** One fresh pytest process per module execution, with the launcher of the module's ordinary Linux lane and the default selection: local-runtime modules through `scripts/ci/run-local-runtime-tests.sh`, storage modules with plain pytest, general modules through `tests.support.ordinary_pytest`. The job prepares no host fixtures and its steps invoke no `sudo`. Selected tests run as they do in their lane; the Linux storage pressure tests, for example, call `sudo` themselves to attach loop devices.
- **Host-fixture tests are not repeated.** `scripts/ci/prepare-local-runtime-fixtures.sh` starts one service session and refuses a second preparation. `test_runner_survives_worker_service_restart[session]` in `tests/plugins/runtime/local/test_service_survival.py` closes that session, so a second execution against the same preparation would fail without any defect in the change. The independent correctness review of this change found this in an earlier revision that repeated host-fixture tests.
- **Schedule.** Rounds over the selection, added modules first, then modified ones by ascending recorded duration; at most 40 rounds. An execution starts only when its expected end lies within a 300-second window. The expectation is the module's longest execution in this job or, before its first one, its duration in `scripts/ci/lane_durations.json`; an unrecorded module counts as the table's median. The table records general lanes only, so every storage and local-runtime module starts from the median, the order among modified ones falls back to their paths, and the window check has no effect before their first execution.
- **Result.** The first failed execution ends the job with an error that names the module, and nothing is retried. A module that never fits the window is named in a warning annotation and in the job summary and does not fail the job. The job summary lists the executions per module and the changed modules that were not selected, and states an empty selection.
- **Gate.** The job has no condition and no dependency. On a `master` push all its steps are skipped and it succeeds. The three protected Scaffold statuses require its result to be `success`; no protected status name changes.
- **Bounds.** One Linux job with Python 3.11.17 under the unchanged ten-minute deadline, `contents: read`, no secret.

Sizing, from the last 80 commits on master before this change and `scripts/ci/lane_durations.json` for `ubuntu-24.04` with Python 3.11.17: 71 commits changed at least one test module, none changed `src/` without changing a test module, and 2 changed only test helpers. A pull request changes 1 test module at the median and 8 at the 90th percentile. One execution of the changed modules takes 6 s at the median, 22 s at the 75th percentile and 92 s at the 90th percentile.

## Diagnostic observation on the #1417 merge

The schedule and execution functions of the job's program, `repetition` and `process_execution` with the default limits, were run on revision `76d651c` in the container described above for the single module `tests/plugins/runtime/local/test_command_capture.py`. The selection step and the lane launcher could not be used there: `76d651c` is a squash commit, not a pull request merge commit, and the container has no systemd. Each execution was `uv run --no-sync pytest -q -p no:cacheprovider <module>`. The third execution failed after 17 seconds in `test_command_capture_without_eof_is_truncated` with `ValueError: invalid literal for int() with base 10: ''`, the #1423 race, and the program stopped without another execution. This is a single observation, not a rate.

## Limits and revisit triggers

- The job finds failures that occur in several percent of single-process executions on a CI runner. It does not establish that a test is stable.
- It does not repeat host-fixture tests.
- It repeats only modules the pull request touches. A source change that destabilises an untouched test, or a change to a test helper or `conftest.py` alone, repeats nothing.
- It runs on Linux with Python 3.11.17 only. A race that exists only on macOS or only on Python 3.15 is not found.
- General-lane modules run with less contention than in their lanes. Revisit trigger: a race in a general-lane module passes this job and later fails in a general lane on master.
- A pull request that touches a module containing an older unstable test fails for a race it did not introduce and has to fix it.
- A module whose single execution does not fit the window is not repeated; the warning names it.
- The job's time is bounded by its setup, the 300-second window and the longest execution whose duration was unknown when it started. In [run 43297550028](https://github.com/realkarych/kora/actions/runs/43297550028), a Linux lane's setup took about 20 s, and the complete Linux local-runtime and storage test steps took 165 s and 244 s. One module cannot take longer than its whole lane, so even an unrecorded module started at the end of the window ends before the ten-minute deadline. Executions have no timeout of their own: a hang ends at the job deadline, as in the lanes.
- Selection, the duration table and the workflow run from the pull request revision, as the ordinary lanes do. A pull request can change them to repeat nothing; review of `tests/changed_test_repetition.py`, `scripts/ci/lane_durations.json`, `tests/scaffold_evidence.py` and the workflow is the protection.

## Evidence from a hosted runner

The pull request that adds the job changes only general-lane test modules. To exercise the local-runtime launcher and a storage module on a hosted runner, its intermediate revision `eeba25f` appended one unused constant to `tests/plugins/runtime/local/test_command_capture.py`, `tests/plugins/runtime/local/test_service_survival.py` and `tests/plugins/storage/test_deadline.py`. The final revision does not contain these edits.

In [run 43330075132](https://github.com/realkarych/kora/actions/runs/43330075132) the job succeeded in 5 min 22 s, of which the repetition step took 303 s:

| Module | Lane | Executions |
|---|---|---|
| `tests/architecture/test_changed_test_repetition.py` | general | 9 |
| `tests/plugins/runtime/local/test_command_capture.py` | local-runtime | 9 |
| `tests/plugins/runtime/local/test_service_survival.py` | local-runtime | 9 |
| `tests/plugins/storage/test_deadline.py` | storage | 10 |
| `tests/architecture/test_workflow_trust.py` | general | 9 |

This is one execution of three special-lane modules. It shows that the launcher works without host-fixture preparation and that these modules pass when repeated; it says nothing about the other local-runtime and storage modules.
