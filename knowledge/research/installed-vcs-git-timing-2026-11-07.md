# Installed VCS Git step timing

Checked on 2026-11-07 for [#895](https://github.com/realkarych/kora/issues/895), the diagnostic step of [#887](https://github.com/realkarych/kora/issues/887). Base `2720ff73`. This record does not attribute or fix the failure in #887. The [record of 2026-11-05](installed-vcs-setup-evidence-2026-11-05.md) stays as written; its bound of two records and 128 bytes is replaced here.

## Source observations

`test_installed_provider_supports_local_tooling[vcs-fixtures_vcs-None-13]` failed on the macOS 15, Python 3.11.17 remainder job in two runs:

| Run | Artifact | Case duration | Preparation evidence |
|---|---|---|---|
| [42089714439](https://github.com/realkarych/kora/actions/runs/42089714439), attempt 1 | 13444509129 | 12.630 s | none, it predates the evidence |
| [42252187432](https://github.com/realkarych/kora/actions/runs/42252187432), attempt 1 | 13522425785 | 12.871 s | `('prepare_entered', 'git_init_remote_deadline')` |

The artifact name of run 42252187432 resolves to cancelled attempt 2, so read attempt 1 by artifact ID.

Among 300 artifacts from 2026-11-01 to 2026-11-06, 270 held `integration.xml`: these two failed and the other 268 passed this case. Across 104 runs on one runner, this case took a median 3.07 s longer than the later replaced-evidence case with the same 13 checks, at most 7.64 s, and longer in 98%. First-import bytecode compilation may account for part of that difference. The failing runners were at the 94th and 99.6th percentile of 270 for the harness case. Tests in the concurrent groups took 1.2 to 2.4 times their median during the failure windows, with no 5 s excess; those offsets are summed from JUnit durations and are uncertain by several seconds.

The fixture's child process has `PATH=/bin:/usr/bin`, set in `src/kora/conformance/command.py`, so on macOS it resolves `/usr/bin/git`, a shim that locates the real Git through `xcrun`. The job's own Git is `/opt/homebrew/bin/git`. This comes from source and a local check, not an observation on a hosted runner.

## Record format

Only with the explicit observation selected, each attempted Git step appends one record between the entry and ending records:

```text
time:<step>:<start_ms>:<total_ms>:<gap_ms>
```

- `start_ms`: from just before starting the command to the time in Git's first `GIT_TRACE2_EVENT` event; `-1` when no event could be read. Both clocks are wall clocks of the same host. Git may open the trace file before taking that time, so a stall creating the file could count here.
- `total_ms`: monotonic duration of the command, including process start and reaping after a deadline.
- `gap_ms`: largest interval between wakeups of a heartbeat thread waiting 50 ms. A quiet process reads about 50, and a command shorter than 50 ms reads its own duration.

Each value is capped at nine digits. The evidence file is bounded at 512 bytes and the reader accepts at most seven timing records of the closed step names. The trace file lives in the fixture's private root and only the numbers leave it. It is removed after the read on the normal path; otherwise, including when a surviving Git descendant recreates it, it goes when the private root is removed. No Git command was added ahead of the measured one; whether that is the first start of `/usr/bin/git` on a runner is not established. Evidence failures stay advisory and do not replace the original Git exception. One exception: if the heartbeat thread cannot start, the step does not run, `RuntimeError` propagates and a `time:` record with `-1` remains.

`integration.xml` holds `vcs_preparation_timing_first` from the ordinary case and `vcs_preparation_timing_later` from the occupied-remote case, which runs later on the same runner. Each is recorded before its test's assertions, so the ordinary case's records survive its own failure. The integration group stops at the first failure, so that failing run holds no later sample and stops at the failed step. The comparison with a later sample therefore comes from passing runs. It uses `start_ms` only, because the occupied-remote command fails early and its `total_ms` is not comparable.

## Reading rule

| Observation | Reading |
|---|---|
| Large `start_ms` or `-1`, small `gap_ms`, and passing runs show a small later `start_ms` | Delay before Git starts: executable start, shim or trace file creation |
| Small `start_ms`, small `gap_ms`, command reaches the deadline | Time is spent inside Git; the record does not say whether it was busy or blocked |
| `gap_ms` comparable to `total_ms` on a command well above 50 ms | Stall at process or host level |

These values are diagnostics, with no numerical gate.

## Local check and limits

On macOS 26.2 arm64 with Python 3.11.17, uv 0.13.1 and the Command Line Tools developer directory, one run of the ordinary case retained `time:init_remote:11:17:17` through `time:read_revision:9:10:10`, and the occupied-remote case retained `time:init_remote:12:13:13`. A local run proves the format works; it says nothing about hosted runners.

`GIT_TRACE2_EVENT` through `/usr/bin/git` is unverified on a hosted runner. If passing hosted runs read `-1`, `start_ms` cannot attribute the delay and the plan in #887 changes first. `gap_ms` sees only stalls affecting the fixture process. A zero deadline in the deadline test proves the failure path, not the cause of a real delay.
