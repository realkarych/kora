# Conformance test acquisition ownership

Checked on 2026-10-28 under [issue #368](https://github.com/realkarych/kora/issues/368).
The [implementation plan](https://github.com/realkarych/kora/issues/368#issuecomment-6475685796)
is published under the owner's standing authorization for routine fixes and verification.
This record describes the prepared FIFO slice; it does not establish completion of the issue.

## Scope and dependency boundary

The initial inspected master is `8d8b766e776e52371921bfdfde2e1955932dfe71`.
The independent slice changes only test code and this evidence record. It protects the
failed-reply, finalization and pending-execution barriers in
`tests/conformance/test_lifetime_io_helper.py`, and the queued-publisher barrier in
`tests/conformance/test_lifetime_pending_helpers.py`.

The recovered-domain handshake fixture, live/pair probe handoff and journal consumers
remain pending. Their shared fixture changes follow the production settlement repair
in [#369](https://github.com/realkarych/kora/issues/369), which depends on
[#359](https://github.com/realkarych/kora/pull/359). No production correction or caller
polling-workaround removal is included in this slice. Already merged #365, #377 and
#380 coverage remains.

## Resource ownership and failure evidence

Each descriptor/directory enters an exit stack immediately after acquisition. Each
helper receives a release callback before channel I/O or a FIFO wait, and each task
receives cancellation/join ownership before the next await. Each FIFO writer is
registered on acquisition. Helpers and local handles are closed before the enclosing
barrier descriptors. The helper's mutation directory is a separate inherited duplicate:
its normal finalizer may close that descriptor before entering its final FIFO wait.

One lazy absolute five-second cleanup budget is shared within each IO scenario. The
pending owner retains its existing injected-clock close path and existing deadlines.
Cleanup failures remain visible while later callbacks still run. Completed task faults
propagate unless their exact exception object was already asserted by the test body.
Signal or settlement refusal still disposes local observation handles and accounts for
the direct child; it does not claim successful process settlement.

Four named expired-barrier cases require exactly `ChannelError` with
`reason is ChannelFailure.DEADLINE`. Their first executions with the release still
registered after the barrier failed at `helper.closed == False`. An independent outer
fallback reaped the actual processes after those assertions. The pending case waits
for the real third helper start before expiring its probe-read budget; an earlier
attempt without that condition failed because it had not established a paused helper.
That earlier attempt is not credited as defect evidence.

The same four boundaries have explicit cancellation cases. The waiter signals an
event immediately before entering its FIFO wait; cancellation must propagate and the
scenario must finish cleanup before the outer fallback. Two further regressions
protect unexpected completed-task faults and local-handle closure after signal refusal.
Both failed before their corrections. Existing successful mutation, transfer, claim
exclusion, queued-publisher and helper-conformance assertions remain.

Every ownership case observes actual child exit/reaping and native process/group
absence. Pending cases also require zero retained trees/processes, pending operations,
cleanup tasks and helper directory/claim descriptors. Garbage collection and exact
`/dev/fd` equality are checked before outer fallback can repair a leak, then again after
removing retained test handles. The existing warnings-as-errors policy stays active.
These exact regression assertions do not establish a process-wide heap/RSS envelope.

## Prepared-slice verification

The three reviewed source inputs are:

| Path under `tests/conformance/` | SHA-256 |
| --- | --- |
| `lifetime_helpers.py` | `efdb9f218c78257ed3a6e863116cb45d7ee37aea4d6676465e5f83f797774805` |
| `test_lifetime_io_helper.py` | `383f87cb16732356b6a9c5494526f3fd3a92378f668b8111a16bb027049a4ea1` |
| `test_lifetime_pending_helpers.py` | `be4b7188079bc035f6b2c7c46ed0eb5155f45ea193344dc7e0aa59a757e4ed48` |

On macOS, Python 3.11.17 with locked dependencies, the two changed test files passed
all 27 cases in 6.64 seconds. Full Ruff lint and format checks passed for 632 files;
strict Pyright reported zero errors and warnings. The local launcher was uv 0.11.7,
while CI pins uv 0.13.1; local results are disclosed with that tool difference.

An earlier six-file selection passed 61 cases in 44.99 seconds, and architecture
passed 734 cases in 41.54 seconds. Both precede the completed-task, refusal and
pre-fallback evidence corrections and are historical checks, not final-source results.
The first integrated IO run failed four reused-directory fixture cases and one missing
import; preserving the separate inherited descriptor and restoring the import corrected
those errors. No deadlines or assertions were weakened to address any failure.

The current two-file command is:

```sh
uv run --no-sync pytest -q tests/conformance/test_lifetime_io_helper.py tests/conformance/test_lifetime_pending_helpers.py
```

Correctness, security, performance and condensation reviews inspected the complete three-file slice
and the retained red/green evidence. Their concrete findings were corrected and the
affected conclusions reassessed independently. Source review is not executable platform
evidence. A fresh Linux aarch64 container with an init reaper, Python 3.11.17 and uv 0.13.1
passed all 63 cases in the required six-file selection in 35.36 seconds. The three
source hashes matched the reviewed macOS inputs; tests ran with networking disabled.
The container did not include `ps`, so no Linux process-listing evidence is claimed;
the tests themselves assert native process/group absence. Final-head CI and the full
issue acceptance remain pending until the dependent work is implemented and checked.

On the same current source, the macOS architecture selection passed all 734 cases in
29.25 seconds. The current six-file macOS selection passed all 63 cases in 37.98 seconds. Filtered
`ps -axo pid,ppid,pgid,stat,args` listings immediately before and after that run
contained no process matching this checkout's interpreter or conformance probe paths.
These listings complement the tests' anchored identity/group assertions, rather than
substituting for them. Raw logs, JUnit and listing data accompany the PR evidence.

No qualification workload, numerical gate, database coverage, supported platform or
hard deadline changes. The historical command-qualification failure and its full
release obligation remain with #395; this work performs no further attribution campaign.
