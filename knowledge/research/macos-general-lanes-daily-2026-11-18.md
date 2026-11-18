# Daily general macOS lanes, 2026-11-18

Checked on 2026-11-18 for [issue #1674](https://github.com/realkarych/kora/issues/1674). It follows the [record of 2026-11-13](macos-general-lanes-on-master-2026-11-13.md) for [issue #1473](https://github.com/realkarych/kora/issues/1473). The numbers are observations of hosted runners over a few days. They are not a portable benchmark and set no numerical gate.

## Decision

On 2026-11-18 co-owner `klukashka` chose, under the delegation recorded in [issue #1356](https://github.com/realkarych/kora/issues/1356):

- The macOS general lanes and the macOS row of `Mutation tooling` run once a day against the current `master` instead of for every push.
- A push to `master` keeps the macOS jobs that pull requests run.
- Failures of the daily run are reported through `Report failed master lanes`.
- A macOS-only defect in a general test is accepted to be found within a day.

The rejected options were to classify a never-acquired macOS lane as an infrastructure outcome with one automatic retry, which changes the rerun rule of AGENTS.md, and to use fewer, longer macOS general lanes, which the ten-minute job deadline limits.

## What failed on master

Between 2026-11-14 and 2026-11-18, 9 of 30 `CI` runs on `master` failed. In each of the eight checked, at commits `c6ac42c1`, `9af8093b`, `38d15f85`, `42fb3840`, `2f070897`, `24e18dfb`, `c8b24201` and `0c4c9ce8`, the only real failures were macOS jobs that GitHub cancelled before any runner started them (`runner_id` 0), with one of these annotations:

- "The job was not acquired by Runner of type hosted even after multiple attempts";
- "Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times".

No test failed in these runs. The other failed jobs were evidence gates that refused a missing lane. The shortage was worst when several pull requests merged within an hour, because their `master` runs competed with pull request runs for the same macOS pool. [Issue #1659](https://github.com/realkarych/kora/issues/1659), opened by `Report failed master lanes`, collected 13 comments from 2026-11-16 to 2026-11-18, all about runner shortages.

## The change

- The `CI` workflow also runs on a daily schedule at 02:17 UTC, away from `Daily master lanes` at 04:41 UTC, which also asks for a macOS runner, and on manual dispatch without inputs.
- A scheduled or dispatched run runs the macOS general lanes, the macOS row of `Mutation tooling` and the macOS row of `Local runtime diagnostics`. Before the change those rows ran for every push. The diagnostics row continues on error and gates nothing.
- A push runs on macOS the static checks, the storage lane and the local-runtime lane, as a pull request does.
- The evidence join and its completion check take the event. A pull request and a push require the macOS platform jobs only. A scheduled or dispatched run requires every macOS lane and the complete-collection partition. Any other event fails.
- `Report failed master lanes` runs on `master` for a push and for a scheduled or dispatched run. It selects the report from the event: `Master push lane failed:` with the evidence job and `Mutation tooling` for a push, and `Daily master CI lane failed:` with the general macOS job as well for the complete run.
- A scheduled or dispatched run has its own concurrency group. With the shared per-revision group, a dispatch on `master` could have cancelled a pending infrastructure rerun of the push run of the same revision, because a newer pending run in a group cancels the older pending one even without `cancel-in-progress`.
- The duration refresh refuses artifacts that lack the general lanes of an environment, so it accepts only a complete run.

## Limits and revisit triggers

- A macOS-only defect in a general test reaches `master` and is found up to a day later, among all merges of that day. Revisit trigger: such a defect that took more than one day to attribute.
- The daily run can itself fail on a macOS runner shortage. It then opens or comments on one issue at most once a day, and is not rerun: `CI recovery` admits only pull request and push runs.
- A push to `master` can still fail on a shortage of the macOS platform jobs, which pull requests also run.
- A workflow file that is not yet on the default branch cannot be dispatched, so the complete run of a change to it first executes after its merge.
- The duration refresh cannot tell from the artifacts on which branch a dispatched run ran; choosing a `master` run is the operator's responsibility.
- Two runs of the same event and revision, such as two dispatches on an unchanged `master`, share a group, so a newer pending one cancels an older pending one.
- The daily run repeats the Linux jobs of the latest `master` push, at the cost of one extra complete `CI` run per day.
