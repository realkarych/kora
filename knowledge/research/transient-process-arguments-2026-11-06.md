# Transient process argument reads

Checked: 2026-11-06. Issue: [#460](https://github.com/realkarych/kora/issues/460). The [plan](https://github.com/realkarych/kora/issues/460#issuecomment-6692141852) preceded edits and names its approver. This record describes evidence, not a new execution-authority or verification policy.

## Natural failure

[CI 41461704082, job 125332187121](https://github.com/realkarych/kora/actions/runs/41461704082/job/125332187121) ran on a push to `master`, head `60e88c54edd1af5503791040080c6ce47aa8e5e9`, macos-15, Python 3.11.17, attempt 1. `tests/measurements/test_local_runtime_cost.py::test_local_runtime_spawn_cost` failed in warm-up retirement `warm-3` with `runner_stop_unconfirmed`. The run concluded `success` because the diagnostics job is `continue-on-error`. Its note was:

```json
{"completed_retention_cycles": 0, "last_observed_runtime.local.retire": {"killed": 0, "matched": 0, "outcome": "listing_incomplete", "scan_failure": "owner_or_argv_unknown", "waited_ms": 0.52275}}
```

Retirements in this workload are strictly sequential, and nothing else emits the event before the note is taken, so the note belongs to the failing call. It places the refusal at the `OWNER_OR_ARGV_UNKNOWN` return of `scan_processes_carrying`, 0.52 ms into the sweep. No deadline expired, no carrier was pinned and no signal was sent. It does not identify the process or the errno, and does not show whether the process was unrelated to the workload.

Earlier failures recorded on the issue retain no subreason and are not relabeled. Since #563 added the subreason, 344 CI runs on pull requests and `master` contain two macOS `runner_stop_unconfirmed` failures: this one, and job 124877220302, which predates that workload's note. The same runs have four Linux local-runtime failures, none containing `runner_stop_unconfirmed`: jobs 125933090750, 127655115947, 127655116115 and 127789049059. They are outside this issue.

## Mechanism

`NativeIdentityReader._darwin_carries` maps `EIO` from `sysctl(KERN_PROCARGS2)`, and `EINVAL` while `PROC_PIDT_SHORTBSDINFO` still reads the process, to `ArgumentMatch.UNKNOWN`. Before this change the scan returned on the first such process, whichever it was, and `verified()` refused on the same result for a dying match.

## Method

Measurements ran on macOS 26.2, Darwin 25.2, arm64, Python 3.11.17, on the revision before this change. Instrumented loops repeated the reader's owner read and argv read in-process and classified each result by errno. Real-reader loops called `NativeIdentityReader` directly. Churn came from spawning `/bin/sleep` and SIGKILLing it about 3 ms later, or from running `/usr/bin/true` to natural exit. Other runs observed only the host's own processes. "Followed" means an unknown process was re-read every millisecond, up to a cap of 1.2 s to 2.5 s, until its result changed.

Runs at load 15–43 overlapped other test suites on the host; those outputs were not retained except as the counts below.

| Measurement | Load 15–43 | Load about 1.6 |
| --- | --- | --- |
| Real scan for a nonexistent vector under SIGKILL churn | 454 of 3,393 scans `owner_or_argv_unknown` | 0 of 4,397 |
| Instrumented scan under the same churn | 30 of 1,149; 30 of 5,255 (load not sampled, falling from high) | 0 of 2,484 |
| Instrumented scan with natural exits as churn | 4 of 945 | 1 of 2,519 |
| Instrumented scan, host processes only | 99 of 1,794 (98 `EIO` without `P_WEXIT`, no duration recorded); 3 of 1,425; 1 of 2,212 (load not sampled, falling from high) | 0 of 2,615 |
| Real `carries()` in a tight loop across SIGKILL, 100 kills | 9 unknown reads | 25 unknown reads |
| Instrumented reads across SIGKILL, 150 kills | `EIO` in 45 and 17 kills | `EIO` in 56 reads; in 21 kills |
| Instrumented reads across natural exit, 150 | `EIO` in 1 | 0 |
| `identities.read()` after `NOTE_EXIT`, 100 kills | 100 absent | 100 absent |
| `identities.read()` during exit, with exit-watch attach | never unknown; every attach failure absent | same |

All 42 followed events resolved, the slowest in 0.74 ms under load, to a readable process or `ESRCH`. Most processes were about 1 ms old or less. `P_WEXIT` at `EIO` was mixed, and an `EIO` process without it was later readable, so `EIO` does not imply exit and cannot be treated as absence.

A read that lands in the window fails at any load. How often a scan lands there varies between runs. Under the same churn the real scan refused 13% of scans at high load and 0% at load about 1.6; the instrumented scan refused 2.6% at high load, 0.57% with load falling and 0% at load about 1.6. Preemption stretching the window and other suites' process churn are both plausible; this data does not separate them.

## Decision

Repeating the read within the existing 1 s bound keeps the fail-closed outcome and moves the verdict to the end of that bound. The alternatives were rejected: treating these errors as absence, because a live process was observed after `EIO`; gating on `P_WEXIT`, because the flag is absent in birth windows; skipping unreadable processes, because their argv is unknown; relying on the fence alone, because it covers runners before `exec` or with rewritten argv rather than a carrier whose argv is merely unreadable; and a smaller repeat budget, because the largest natural class has no duration data.

## Limits

- Darwin 25.2 only. On macos-15 the errno and the window length are unmeasured, and the natural failure does not show that its unknown read would have resolved. `test_carries_settles_across_real_exits` records its slowest settle as a JUnit property on each supported platform.
- "Transient" rests on 42 followed events, few at high load. The largest natural class has no duration data. A longer unknown still refuses, so a wrong estimate keeps the fail-closed outcome and costs a remaining refusal after holding the sweep worker for the bound.
- Linux reaches the same repeat path but its unknown routes are narrow, and nothing here measures a window there.
- One historical failure of `test_starting_entry_resolves[too_many_matches_are_unconfirmed]` lost its assertion detail and did not reproduce in 12 runs under churn. Source shows that a transient unknown gives `("listing_incomplete", 0, 0)` where that leg expects `too_many_matches`, and that failure overlapped heavy host load. A 1 s deadline expiry is unsupported (scan mean 7.5 ms, maximum 14.3 ms) but cannot be excluded for it.
- Ordinary CI has little power to show the repair, because one macOS diagnostics failure stands against 321 successes since #563. The evidence for the repair is the deterministic regressions and the real-kernel test.

## Regressions

- `test_starting_entry_resolves`: `transient_unreadable_reads_do_not_refuse`, `dying_match_is_dropped`, `identity_change_during_reread_is_not_signalled`, `persistent_unreadable_match_is_unconfirmed`, `persistent_unreadable_bystander_refuses_and_keeps_fence`, `queued_sweep_behind_held_reread`.
- `test_local_runtime_releases_resources`: `close_during_scan_reread_is_released`, `close_during_pinning_reread_is_released`, `cancelled_retire_during_scan_reread_is_released`, `cancelled_retire_during_pinning_reread_is_released`.
- `test_platform_mechanisms`: `unknown_argv_settles_within_the_deadline`, `expired_deadline_still_reads_once`, `unencodable_vector_reads_nothing`, `unknown_at_the_deadline_ends_the_scan`, `far_deadline_is_capped_by_the_scan_bound`, `forked_reader_is_unknown`.
- `test_carries_settles_across_real_exits`: real children on the real kernel. It proves settling, not settling within 1 s of real time, and proves nothing about this window on Linux.
