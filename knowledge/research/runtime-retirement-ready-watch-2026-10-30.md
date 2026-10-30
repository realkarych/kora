# Retirement after a completed exit watch

Checked: 2026-10-30. Implementation issue: [#508](https://github.com/realkarych/kora/issues/508), found during [#460](https://github.com/realkarych/kora/issues/460). The [bounded plan](https://github.com/realkarych/kora/issues/460#issuecomment-6531700664) preceded edits. This record describes evidence, not a new execution-authority or verification policy.

## Proven boundary

At base `49e579b79182deee3e1f82bde3800bf8aefe964e`, `LocalRuntime.pin()` opens an exit watch before verifying a carrier's identity and complete argv. The verifier can capture two equal live identities, then the process can exit before its executor result reaches the event loop. The completed-watch branch unconditionally raises `MATCH_UNREADABLE`, even when a fresh observation would prove the process absent.

The repair uses the existing `vanished()` check through the existing sweep executor. Fresh absence or death drops the carrier. An unreadable result or any live process refuses, including a changed identity. This branch never signals. A completed watch alone is insufficient: it can belong to an earlier occupant of the PID.

Identity/UID/argv verification, scan and grace limits, admission, executor and watch ownership, fence-before-sweep and receipt persistence retain their existing contracts. Runtime diagnostics keep the existing count-only `runtime.local.retire` event and field allowlist; no new process identity, argv or content is logged. Public APIs, persisted formats and dependencies are unchanged.

## Deterministic regression

`tests/plugins/runtime/local/test_starting.py::test_ready_exit_watch_requires_fresh_disappearance` uses a real carrier, the actual `pin()` exit watch and the configured `SteeredIdentityReader` protocol implementation. It pauses the second native live read with thread/event-loop barriers, kills the owned child, waits for actual watch completion and only then releases the result. No watcher mutation, timing sleep or OS PID-reuse claim is involved.

| Fresh observation | Source | Required result |
| --- | --- | --- |
| Absent | Native read after the child is reaped | No pinned carrier |
| Dead | Native disappearance is checked first; the configured reader supplies a dead snapshot of the observed identity | No pinned carrier |
| Unknown | Configured reader after actual watched exit | `MATCH_UNREADABLE` |
| Live, same identity | Retained live observation through the configured reader | `MATCH_UNREADABLE` |
| Live, changed identity | Configured reader changes the observed start identity | `MATCH_UNREADABLE` |

The dead snapshot is explicit because macOS can report an unreaped exited child as absent. The test does not mislabel that fake observation as native evidence. Every case asserts exactly three identity reads and restores descriptor and zombie counts. The caller-owned anchors and executor are closed even if the test fails or is cancelled. Existing public retirement cases retain fence, decoy, concurrent-call, receipt, cancellation and grace coverage.

On local macOS/Python 3.11.17, the initial absent case failed at the unchanged unconditional refusal after watch completion and reaping, establishing the red criterion. The first post-fix selection passed 16 cases and failed the test's incorrect assumption that an unreaped child must remain visible as dead. Correcting that platform assumption produced 1,080 passing runtime and architecture tests; eight existing host-fixture/measurement cases were outside the normal local selection. Supported-platform final-head CI, including those applicable fixtures and the unchanged spawn-cost workload, remains required. The PR records exact revisions, commands, artifacts, strict-check results and independent correctness/security review.

## Historical attribution limits

The macOS spawn-cost failure in [run 40980592112](https://github.com/realkarych/kora/actions/runs/40980592112) and the ordinary starting-entry failure in [run 41112544809](https://github.com/realkarych/kora/actions/runs/41112544809) both report `runner_stop_unconfirmed`, but neither retained a refusal stage sufficient to attribute it to this branch. The latter's JUnit case lasted 0.460 seconds and has no last-observed event note. Timing alone does not establish causality.

The deterministic regression proves this source defect and its repair, not the historical failures' exact causes. Those failures remain failures and #460 remains open. A later ordinary CI success is new execution evidence, not retroactive repair of a failed run. No unchanged campaign, retry loop, raised timeout or removed safety assertion is part of this change.
