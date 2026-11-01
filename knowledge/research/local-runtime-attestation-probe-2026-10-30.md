# Research: peer attestation on the local runtime's control socket

Checked on **2026-10-30** for issue #315 with CPython 3.11, the standard library and `ctypes` only. A listening Unix socket accepted a connection from a child process, and the child connected and read the listener's attestation:

- Linux 6.12 on arm64 (a container on a Linux virtual machine), CPython 3.11 from the official `python:3.11-slim` image;
- macOS 26 on arm64, CPython 3.11.

The [mechanisms probe of 2026-10-22](local-runtime-mechanisms-probe-2026-10-22.md) checked these options on a socket pair only; this record covers an accepted listening socket and the connecting side, which section 2 left to the runner work item.

## Linux

| mechanism | observed |
| --- | --- |
| `SO_PEERPIDFD` (option 77) on the accepted socket | returns a pidfd whose `fdinfo` `Pid:` line is the connecting child's pid |
| `SO_PEERPIDFD` on the connecting side | returns a pidfd of the listening process |
| `SO_PEERCRED` | pid, uid and gid of the peer on both sides |
| the pidfd after the peer exits | polls readable, and `fdinfo` then reports `Pid: -1` |
| `SO_PEERPIDFD` read only after the peer exited | fails with `EINVAL`; a socket pair still supports the option, so detection on a socket pair is unaffected |
| start time | `/proc/<pid>/stat` field 22 in ticks of `SC_CLK_TCK` (100) is earlier than `CLOCK_BOOTTIME` read right after `accept` |

## macOS

| mechanism | observed |
| --- | --- |
| `LOCAL_PEERPID` (`SOL_LOCAL` 0, option 2) on the accepted socket | the connecting child's pid |
| `LOCAL_PEERPID` on the connecting side | the listening process's pid |
| `LOCAL_PEERCRED` (option 1) | a 76-byte `xucred`, version 0, with the peer's uid |
| `LOCAL_PEERPID` read only after the peer exited | fails with `ENOTCONN` |
| start time | `proc_pid_rusage(RUSAGE_INFO_V0)` `ri_proc_start_abstime`, at byte offset 80, is in `mach_absolute_time` units and earlier than `mach_absolute_time` read right after `accept` |

## Use

`kora.runner.peers` chooses `SO_PEERPIDFD`, `SO_PEERCRED` or `LOCAL_PEERPID` with `LOCAL_PEERCRED` once per process and refuses any per-connection failure, including the two failures after a peer's exit above. The GitHub-hosted runners of the `local-runtime` lane exercise the same paths through `test_control_channel_rejects_impostors` on every change.

## Implementation measurements on 2026-10-31

The #315 implementation was measured on macOS arm64 with CPython 3.11.17 using `uv run --no-sync pytest -q -m measurement tests/measurements/test_local_runtime_cost.py`. All three measurement tests passed. These are local diagnostics from an uncommitted implementation based on `b5524ea`, not final-revision CI evidence or performance guarantees.

The attestation workload used 10 warm-up connections followed by 100 measured connections in each sequential and eight-concurrent series. Median kernel attestation took 0.020 ms sequentially and 0.029 ms concurrently. Worker status calls had a median of 0.545 ms and p95 of 0.708 ms. All three workloads reported zero net open descriptors. Across 100 status retention cycles, traced component allocations increased by 628 bytes in the first interval and zero bytes in each of the next 19 intervals after garbage collection. This does not measure native or untraced allocations.

The macOS host-fixture tests `other_uid_member` and `setuid_carrier_is_not_owned` failed: the observations were `ABSENT` instead of `UNKNOWN` and `DIFFERENT`, respectively. Both failures reproduced against unchanged upstream revision `2b468c836e381e1b487effa5d455b5e74bb53d01`. They remain failed checks; the baseline reproduction does not establish the host-fixture security guarantees.

## Shutdown and attestation regression coverage

The implementation adds deterministic real-resource cases to `test_local_runtime_releases_resources`: `shutdown_releases_pending_rebind`, `cancelled_shutdown_releases_pending_rebind`, `shutdown_at_status_connect`, `cancelled_runtime_close_drains_attestation` and `cancelled_supervisor_releases_resources`. The rebind barrier holds an already-created real listener/anchor until shutdown begins; the tests verify actual descriptor closure, task/timer release and retained registry state. The supervisor case executes the real composition root in a separate process and cancels it twice while a connection remains open.

`test_platform_mechanisms` adds kernel source selection, platform clock comparisons, process exit during identity reading and refusal without downgrade after a failed peer option. `test_logging_redacts_content_and_safe_exception_chains` verifies DEBUG content exclusion, tick coalescing and final flush. `test_logging_sink_failure_does_not_stall_work` verifies status and shutdown with a broken pipe sink. The non-reading status case proves connection release before the client reads the small reply; separate `BoundedChannel` tests own transport backpressure and write-deadline behavior.

The Linux arm64 local-runtime/runner suite on tmpfs passed 324 cases with 11 privileged host-fixture cases deselected; the subsequently added real-supervisor cancellation case passed separately. The macOS runtime/runner/architecture suite passed 1,229 cases with eight privileged cases deselected, followed by the real-supervisor case. These results do not claim Linux host-fixture coverage. An initial Linux container run on overlayfs failed the existing filesystem-type check; moving the test temporary directory to tmpfs corrected the environment without changing that assertion.

After the binder seam was introduced, fresh serialized macOS diagnostics passed all three measurement tests in 15.05 seconds. Worker status median/p95 were 0.472/0.538 ms; all descriptor deltas remained zero. The traced status-retention result remained +628 bytes initially, followed by 19 zero intervals. The earlier measurements above keep their original provenance.

## macOS host-fixture repair

The original helper was a relocated, OS-signed `sleep`. On the tested macOS host it exited with SIGKILL before identity observation; `ps` showed a defunct process with effective/saved UID `nobody` and the original user's real UID. Signature verification reported `CSSMERR_TP_NOT_TRUSTED`. A temporary copy was killed both before and after ad-hoc signing. A locally compiled native sleeper executed successfully without privilege, establishing a candidate fixture repair, not yet its cross-UID evidence.

The owner approved the [bounded native-helper addition](https://github.com/realkarych/kora/issues/315#issuecomment-6547589972). On macOS, fixture preparation now uses `/usr/bin/cc` to compile `scripts/ci/setuid-sleep.c`, which accepts one duration of at most 300 seconds, then installs the helper as `nobody` with mode `4755`. The temporary build directory is removed on success or failure. Linux retains the existing system `sleep` copy. This introduces a macOS test-preparation toolchain prerequisite, not a runtime dependency or an acceptance-gate change.

After installing the native arm64 helper, the complete affected macOS suite passed: `uv run --no-sync pytest -q --tb=short -m 'not measurement' tests/plugins/runtime/local tests/runner tests/architecture` reported 1,238 passed in 45.91 seconds, including `other_uid_member` and `setuid_carrier_is_not_owned`. The installed helper belonged to `nobody` with mode `4755`. This supplies the previously missing cross-UID evidence; historical failures retain their original results. Fixture teardown completed: `/opt/kora-local-runtime` is absent, no fixture mount remains, and `hdiutil info` no longer lists its RAM disk. Final-revision Linux/macOS CI remains required after publication, and owner review of the `docs/` wording remains a merge condition.

## CI shutdown corrections on 2026-10-31

[PR #571 CI](https://github.com/realkarych/kora/actions/runs/41232806843) on head `b22b7ba` exposed two Linux failures. Closing executor admission before scheduling observation cancellation allowed an admitted status request to report `runtime_closed` instead of cancellation. Cancellation now happens synchronously before admission closes; cleanup still drains the admitted tasks. The regression observes the public current task through an injected clock and verifies cancellation before deferred cleanup runs.

A separate deterministic real-socket regression, `shutdown_during_expiration_cleanup`, proved that cancellation during first-frame timer cleanup could bypass the accepted socket's close. A nested `finally` now closes the socket even when timer cleanup is interrupted. Both production regressions failed against the previous behavior and passed with the corrections.

The exited-peer test previously assumed every Linux kernel rejects `SO_PEERPIDFD` after the peer is reaped. The corrected test first probes the actual kernel result, closes any returned descriptor and requires the corresponding exact refusal: `UNREADABLE` for an option failure or `EXITED` for an already-exited pidfd. Zero identity reads remains mandatory, preserving the fallback discriminator. The process-exit-during-attestation test remains separate.

Local corrected-snapshot evidence: macOS lifecycle/platform/no-suppression checks reported 63 passed and two privileged fixture cases deselected in 21.93 seconds; Linux/Python 3.11.18 targeted cancellation and peer-exit checks reported seven passed and 41 deselected in 0.43 seconds. Ruff lint/format, strict Pyright and diff checks passed. An earlier Linux container run reported 21 failed and 24 passed because its missing `/etc/machine-id` made runtime lineage unavailable; that failed environment run is retained, and only container prerequisites changed before the targeted run. These results do not replace final-revision Linux/macOS CI.
