# General macOS lanes on master pushes, 2026-11-13

Checked on 2026-11-13 for [issue #1473](https://github.com/realkarych/kora/issues/1473), part of [issue #1364](https://github.com/realkarych/kora/issues/1364). The numbers are observations of hosted runners over one week. They are not a portable benchmark and set no numerical gate.

## Owner decisions

On 2026-11-13 the project owner decided:

- A pull request runs on macOS only the static checks, the storage lane and the local-runtime lane.
- The general test lanes run on macOS for every push to `master`.
- When a general macOS lane fails on a push to `master`, a job of the `CI` workflow opens an issue or comments on the open one. The owner chose this over moving the lanes to the daily workflow, which would find a failure up to a day later among all merges of that day, and over no notification.
- The same job also reports the shared evidence job and the `Mutation tooling` job. The owner added this after both independent reviews of the change showed that two checks had become push-only without a notification: the evidence join's collection, assignment and partition checks for macOS, and the macOS row of `Mutation tooling`.

## Why pull requests were slow

Every `CI` execution requested seven macOS scaffold jobs: the static checks, the storage lane, the local-runtime lane and four general lanes. Since [issue #1278](https://github.com/realkarych/kora/issues/1278) at most two of them run at a time, because the hosted pool did not supply four macOS runners at once.

| macOS scaffold job | Median duration on 2026-11-12 |
|---|---|
| `static-checks` | 166 s |
| `storage` | 304 s |
| `local-runtime` | 321 s |
| `general-1` … `general-4` | 271–315 s each |

Two at a time, these jobs need about 16 minutes of wall-clock time before any wait for a runner. Complete executions took 25–52 minutes on 2026-11-12, and 28.5 and 36.1 minutes in the two executions measured on 2026-11-13, against the ten-minute feedback target. On 2026-11-12 the median wait of a macOS job for its runner was 511 s, against 17 s on Linux, and 45 macOS jobs were cancelled without ever acquiring one.

## What the general lanes found on macOS

Method: the 700 `CI` runs between 2026-11-07 17:42 and 2026-11-13 08:27 UTC. 379 of them failed or were cancelled. In 92 test lane jobs a test step failed, 36 on macOS and 56 on Linux. The job logs of 55 runs name the failing tests. A pair below is one test in one run that failed on macOS and not on Linux in that run.

| Directory | macOS-only pairs |
|---|---|
| `tests/plugins/storage` | 15 |
| `tests/plugins/runtime` | 7 |
| `tests/conformance` | 6 |
| `tests/client` | 3 |
| every other directory | 0 |

The storage and runtime pairs come from the storage and local-runtime lanes, which stay in pull requests. One of them was a defect caught before its merge: the `test_command_observation` tests failed only on macOS in the pull request that added them.

The nine general-lane pairs are `test_command_listeners_stay_in_the_registered_child_tree` (3), `test_command_outcome_precedes_terminal_report_and_journal_removal`, `test_scoped_cleanup_does_not_wait_for_command_outcome`, `test_live_probe_readiness_timeout_retains_joined_diagnostics`, `test_client_classifies_connect_failure_before_send` (2) and `test_client_releases_requests`. They recur across unrelated runs, as unstable tests do. None belongs to a change that broke macOS.

The general macOS lanes hold 43.3 minutes of test time in 482 modules, by `scripts/ci/lane_durations.json`. 428 modules with 30.0 minutes start no process, send no signal and check no platform in their own source text. That classification looks only at the test files, so it is an upper estimate of platform-neutral work.

## The change

- `tests/scaffold_evidence.py` plans the macOS general rows as a matrix of their own, `macos_general`.
- `.github/workflows/ci.yml` runs them in `scaffold-tests-macos-general`, only for pushes, after `scaffold-tests-macos` has finished and with the same limit of two concurrent jobs.
- The evidence join takes the event. For a pull request it expects exactly the storage and local-runtime lanes of macOS and requires the general macOS job to be skipped. For a push it expects every lane and requires the job to succeed. An unknown event fails.
- The check that all lanes of an environment equal one complete collection uses a general lane's collection. For macOS it therefore applies on pushes only.
- `Report failed master lanes` runs for pushes to `master`, holds `issues: write` and calls `scripts/ci/scheduled_report.py master-push`. It reports three results: the general macOS job, the evidence job with its proof, and `Mutation tooling`. The program's text and tracker are those of the daily report.
- The macOS rows of `Mutation tooling` and `Local runtime diagnostics` are chosen by event and run only for pushes.

Expected effect: three macOS scaffold jobs of 166 s, 304 s and 321 s, two at a time, need about 8 minutes instead of about 16, before runner waits. Runs on `master` keep their duration.

## Limits and revisit triggers

- A macOS-only defect in a general test reaches `master` and is found on the next `master` run. Revisit trigger: such a defect that a pull request lane would have caught.
- For macOS in pull requests the join no longer proves that the lanes cover one complete collection. It still proves each lane's receipt, revision, run attempt and the named local-runtime host-fixture tests.
- The unstable general tests listed above will open or update the issue on `master` at their current rate until they are fixed.
- A job cancelled because no runner was acquired is reported like a failed one, before any permitted infrastructure rerun.
- The evidence job fails whenever a lane fails. A failed general macOS lane therefore updates two issues, and a failed Linux lane on `master`, which pull requests also run, opens or updates the `Scaffold evidence` issue. The `Mutation tooling` result covers its Linux row as well.
- The macOS row of `Local runtime diagnostics` continues on error, so its failure on `master` does not turn the run red.
- The job that runs only for pushes cannot be executed by a pull request. Its first execution is the first `master` run after the merge.
- The limit of two concurrent macOS scaffold jobs is per workflow run. Several runs at once still request more.
