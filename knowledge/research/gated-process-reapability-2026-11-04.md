# Gated-process reapability on Darwin

Checked 2026-11-04. Implementing issue: [#436](https://github.com/realkarych/kora/issues/436). The commands below ran on the uncommitted sources based on `4e2ca8056388d955349e6513d5305f62f177b255`. That tree was then rebased, without a source conflict, onto `f88549f6c44f080856c47665971f72624723a9c8`. The approved plan is the issue comment that selects the private readiness gate: keep `ExitAnchor` and ordinary identity reads, and make `GatedProcess.settle` for a Darwin direct child wait until a non-consuming zombie probe and a fresh domain observation both succeed. This record is local implementation evidence for that plan. It does not replace final-head Linux and macOS CI or independent review of the committed diff.

## What was checked

The probe reuses `BsdInfo` and `proc_pidinfo(PROC_PIDTBSDINFO)` with argument 1. It requires a complete registration and the same pid, start time, process group and current parent. Status 5 is ready. Any other full-size match is not ready. A short result, including the `ESRCH` window, is unknown and is not treated as ready. The probe does not call `getsid`.

`GatedProcess.settle` arms one shared child-change watch before the probe when the child is not yet known to be a zombie. A not-ready child may wait only while the fresh domain observation is live or settled. A ready child still waits while the domain is live, or while an existing member watch can still change an incomplete domain observation. Success requires both a zombie and a fresh settled domain observation. Unknown, mismatched or failed probe and watch results, an unknown anchor, and a domain observation with nothing left to wait for refuse. `release` remains the only consuming wait. Linux composition passes no probe, so it keeps the existing `waitid` `WNOWAIT` path and does not open the extra watch.

Hand-written production changes, counted as added plus deleted lines under `src/kora` and including the new `kora.runner.child_wait` module, total 455 against merge base `6b9edf914e7097f10abb4efb713ebe7930124b86`. The direct-child probe imports `ProcessRegistration` from `kora.protocols.lifetime_records`, so importing the IO helper does not load the lifetime package. Tests, the lifetime specification, the module map and this record are excluded.

## Commands and results

Local host: macOS, the developer machine used for this check. Python 3.11.17 from the project environment. Dependencies were installed from the locked set so the checks could import the project; `uv.lock` was not edited.

| Command | Result |
|---|---|
| `uv run --no-sync ruff check --ignore-noqa` on `src/kora/runner/child_wait.py`, `src/kora/runner/processes.py`, `src/kora/runner/identity.py`, `src/kora/conformance/io_helper.py`, `src/kora/conformance/lifetime.py`, `src/kora/conformance/command.py` and `tests/runner/test_gated_process_settlement.py` | Passed. |
| `uv run --no-sync pyright` on the same files | 0 errors, 0 warnings, 0 informations. |
| `uv run --no-sync pytest -q tests/runner/test_gated_process_settlement.py tests/conformance/test_local_process_lifetime.py tests/conformance/test_lifetime_io_helper.py tests/architecture/test_import_layers.py` | 93 passed in 9.73s. |
| `uv run --no-sync pytest -q tests/architecture/test_no_suppression.py` | 18 passed in 6.18s. |

The settlement file covers a real gated child held not-ready under both a live domain and a listed-absent settled domain, then released once; spurious and already-complete watch hints; probe, identity, watch, anchor, capacity and deadline refusals; two settlers sharing one watch across cancellation and parent release; domain liveness after a zombie probe; capture remaining until release; one consuming `waitpid`; the moved watch on this host; the injected and native Darwin probe; and a command outcome that stays complete across the transition. The lifetime file includes the existing descendant and group-anchor settlement cases. The helper file covers the watch after it moved. Import-layer and no-suppression checks passed for the new module and the edited sources.

## Limits

Linux was not executed here. The Linux direct-child path is the unchanged domain loop because the native factory returns no probe and no extra watch. macOS CI still has to run the same tests. Design review of the plan does not review this diff. No new dependency, persisted format, public operation, platform or kernel-latency guarantee is claimed. A short `proc_pidinfo` result remains a refusal. An unrelated `SIGCHLD` can cause another observation pass; that is not a bound on CPU use.
