# Conformance test acquisition ownership

Checked on 2026-10-28 under [issue #368](https://github.com/realkarych/kora/issues/368).
The [implementation plan](https://github.com/realkarych/kora/issues/368#issuecomment-6475685796)
is published under the owner's standing authorization for routine fixes and verification.
This record describes the FIFO/helper/task slice tracked by [issue #416](https://github.com/realkarych/kora/issues/416). Parent #368 remains open for its other named ownership boundaries.

## Scope and dependency boundary

The initial inspected master is `8d8b766e776e52371921bfdfde2e1955932dfe71`.
The independent slice changes only test code and this evidence record. It protects the
failed-reply, finalization and pending-execution barriers in
`tests/conformance/test_lifetime_io_helper.py`, and the queued-publisher barrier in
`tests/conformance/test_lifetime_pending_helpers.py`.

The recovered-domain handshake fixture, live/pair probe handoff and journal consumers
remain pending in #368. Their shared fixture changes follow the production settlement repair
in [#369](https://github.com/realkarych/kora/issues/369). Its dependency
[#359](https://github.com/realkarych/kora/pull/359) was accepted on 2026-10-29. No production correction or caller
polling-workaround removal is included in this slice.
Coverage from already merged #365, #377 and #380 remains.

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

The four expired-barrier and four cancellation cases observe actual child
exit/reaping and native process/group absence. The completed-task regression
asserts helper closure, direct-child return status, exception context and descriptor
equality; it does not add a native group scan. The signal-refusal regression proves
local-handle closure while deliberately retaining unsettled process state, then its
independent emergency finalizer stops and reaps the direct child. Pending cases also require zero retained trees/processes, pending operations,
cleanup tasks and helper directory/claim descriptors. Garbage collection and exact
`/dev/fd` equality are checked before outer fallback can repair a leak, then again after
removing retained test handles. The existing warnings-as-errors policy stays active.
These exact regression assertions do not establish a process-wide heap/RSS envelope.

## Original prepared-slice verification

The original prepared snapshot used provider inputs from
`8d8b766e776e52371921bfdfde2e1955932dfe71` and the three reviewed test inputs below.
It was committed as `c14883dec3c36d4f1c22391c911944146286543e`; the subsequent
`91bf7237ddf44be6015288e8ce24ccafb562961a` changed evidence wording only.

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

The changed-file command is:

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
the expired-barrier and cancellation tests assert native process/group absence.
Hosted CI [40738242028](https://github.com/realkarych/kora/actions/runs/40738242028)
subsequently passed for `91bf7237ddf44be6015288e8ce24ccafb562961a` and its execution's
merge inputs. It completed in 48 minutes 48 seconds including scheduling and dependent
jobs, exceeding the ten-minute feedback target while passing the hard checks.
Complete issue acceptance remains pending the dependent implementation.

On that original prepared snapshot, the macOS architecture selection passed all 734
cases in 29.25 seconds. Its six-file selection passed all 63 cases in 37.98 seconds. Filtered
`ps -axo pid,ppid,pgid,stat,args` listings immediately before and after that run
contained no process matching this checkout's interpreter or conformance probe paths.
These listings complement the tests' anchored identity/group assertions, rather than
substituting for them. The original local logs, JUnit and listings were inspected
before temporary-directory cleanup; they are no longer available locally. Their
recorded observations remain historical evidence, while hosted CI retains its own
execution evidence. No old local run is relabeled as an execution of later inputs.

## Accepted-master integration on 2026-10-29

Merge source `63e0bb301bc97d7344fb2b1e39653aafbcc2b059` integrates accepted master
`e6c0730f1ebdd0e7d809e8f8a7780034cb0aba97`. The three test hashes above remain
unchanged, but their identity, filesystem and channel providers changed. Earlier local
results therefore do not establish compatibility with this integrated source.

Fresh macOS checks on that merge source passed:

- The exact six-file issue selection: 63 cases in 38.10 seconds.
- `uv run --no-sync pytest -q tests/architecture`: 800 cases in 30.84 seconds.
- Full Ruff lint and format checks: 650 files; strict Pyright: zero errors or warnings.

The local interpreter and launcher remain Python 3.11.17 and uv 0.11.7. Filtered
process listings before and after the new six-file run again contained no matching
interpreter or probe process. The new checks do not establish Linux or final-head
hosted results; those require the integrated PR's current CI.

Independent correctness, security and performance reassessments inspected only the
changed provider edges consumed by this slice: observer-scoped native identity;
claim/journal calls into private-file helpers; and existing byte-frame/record writes
after the memoryview type extension. Each passed. Unchanged test ownership, exception
visibility and descriptor-method conclusions carry with their original inspected
inputs. Unconsumed typed-frame methods and unrelated runtime, registry and storage
behavior did not expand the assignment. Condensation inputs are unchanged.

The complete own diff against the accepted master still contains zero production
lines. General recovered-domain setup, probe handoff and journal-consumer work remain
unimplemented behind #369 and #359. No relevant master input or coordinator-owned
branch or campaign was changed by this integration.

No qualification workload, numerical gate, database coverage, supported platform or
hard deadline changes. The historical command-qualification failure and its full
release obligation remain with #395; this work performs no further attribution campaign.

## Adverse storage-task fixture on 2026-10-29

The integrated-head [macOS conformance job](https://github.com/realkarych/kora/actions/runs/40817993287/job/123263537325)
failed `test_storage_case_runner_retains_failed_domain[task]`: its deliberately pending
task was collected before the case runner inspected live tasks. The child logged
pending-task destruction and then failed its expected refusal assertion. This job
reported one failure and 454 passing cases; its [native evidence](https://github.com/realkarych/kora/actions/runs/40817993287/artifacts/12873363303)
remains failed. Requested head was `cceb0ec0645e6e8e3f0872ab9ed7c175b402f072`;
the actual checkout was `74cfae49df54c54762d8c3485515e312529df410`.

The [bounded plan amendment](https://github.com/realkarych/kora/issues/368#issuecomment-6488174277)
adds explicit task ownership only in `tests/conformance/storage_case_probe.py`.
Both direct and late adverse tasks enter the adapter's typed strong collection.
Each terminal mode creates at most one; successful modes add none and failed-sequence
refusal prevents another exercise. The existing parent-owned process teardown ends
their lifetime. Remaining tasks still require `TASKS_LIVE`, failed-domain retention
and actual child closure/reaping under the existing deadlines.

The direct task case now waits for a task-entry event before collecting garbage.
Without retention, one controlled execution reproduced the CI failure in 2.04 seconds.
After retention, the complete parent test file passed all 14 cases in 30.55 seconds.
The forced collection stays in the regression. No task is cancelled to turn the
adverse scenario into successful case cleanup.

Independent correctness and resource-ownership reviewers inspected proposal, actual
fixture delta and red/green evidence, then [returned passing scoped results](https://github.com/realkarych/kora/pull/406#issuecomment-6488271178).
The inspected probe SHA-256 is
`c60497f95cbc77e8989cc809c6ef78c8c19af7ac3194dd581fc9db6d4f32dabe`.
Full Ruff lint/format passed for 650 files, strict Pyright reported zero errors or
warnings, and architecture passed 800 cases in 28.23 seconds. Fresh final-head
supported-platform CI remains required. Earlier unchanged six-file inputs retain
their original execution revisions rather than becoming new full-suite results.

The production runner, parent assertions, contracts, gates, platforms and dependencies
are unchanged. There is no new execution or signal authority. The correction contains
zero production lines and does not start the dependent #369 or shared #368 work.

## Remaining recovered-process ownership on 2026-10-30

The remaining [#368 plan](https://github.com/realkarych/kora/issues/368#issuecomment-6511183830)
resumes from merged master `9731b17908e5bf71e44c1546cec8bdafffebd474` after
[#450](https://github.com/realkarych/kora/pull/450) delivered recovered settlement.
The [original plan](https://github.com/realkarych/kora/issues/368#issuecomment-6475685796)
and [accepted split](https://github.com/realkarych/kora/issues/368#issuecomment-6494259231)
retain the completed FIFO/task slice above. The
[independent design assessment](https://github.com/realkarych/kora/issues/368#issuecomment-6510780007)
covers the remaining private authority transfer and native acquisition boundaries.
This change has zero handwritten production lines; its scope is nine existing Python
test/support files and this record. No product contract, identity, signaling rule,
dependency, supported platform, timeout or verification gate changes.

### Ownership boundaries and retained behavior

Live/pair probes publish one private typed record containing the invocation ID and
all existing `ProcessRegistration` values, limited to two registrations and 64 KiB.
The caller reads through cancellable native pipe readiness under one acquisition
deadline. Complete identity, mode cardinality and unique process/acquisition identities
are checked before every returned observer and its independent disposal/release
callback are registered synchronously. Only then is the exact stdin acknowledgment
written. The probe retains its real `ParentLifetime` until that acknowledgment.
Rejected or partial records and a failed acknowledgment grant no signaling authority:
caller cleanup closes stdin and joins probe-owned cleanup first. The probe checks
actual process absence and empty groups before exiting after unacknowledged cleanup.
Tree/gone output remains compatible; the live workload remains `signal.pause()`.

The nine live/pair acquisition sites now obtain this authority before journal lookup
or parsing: parent-exit and recovery-claim each have one, journal-recovery has five,
and resume and parent-lifetime each have one. Existing sibling, semantic-corruption,
inventory, expired-observation, capacity, claim and diagnostic assertions remain.
Invalid-first-parse regressions in parent-exit, recovery-claim and journal-recovery
write invalid bytes before the first `JournalSnapshot` parse and require exactly
Pydantic `ValidationError`. Every independently reported domain is still closed;
the invalid bytes remain evidence and never supply kill authority.

The recovered-domain fixture reuses #369's staged controls and owned socket stack
for legacy normal/resistant/family setup too. Listener ownership precedes bind/listen;
direct child/anchor ownership precedes accept; accepted controls precede recv; family
creation follows retained leader authority. Cleanup closes the complete control set
before waiting for the reaper's retained children. This is composite control closure,
not a claim that stdin EOF alone interrupts every synchronous child wait. Early
identity-receive failure cannot supply a parent registration, so the direct reaper
checks its collected child and freshly empty native group before emitting one fixed
private stdout byte. The parent requires that witness in the early recv regressions.
The intentional reaped-leader/live-member case uses controlled leader death only
after member authority is retained, and emits no false empty-group witness.

One lazy actual-clock `CleanupDeadline` covers native finalizers and sibling cleanup.
FIFO setup keeps its separate original acquisition wait. Converted owner disposal
uses the remaining actual cleanup budget while preserving its injected decision
clock; unchanged callers retain their original defaults. Resume channel/anchor and
parent-owner finalizers run even when an earlier observer signal or settlement fails.
The existing bridge `finish_cleanup` owns a retained shielded task around the complete
join/release stack and domain teardown. Repeated caller cancellation cannot abandon
that task; bounded event controls exercise pre-acknowledgment join, post-acknowledgment
release and domain cleanup, preserving the original cancellation and deadline.

### Local verification and retained failures

Local environment: macOS 27.2 arm64, CPython 3.11.17, uv 0.11.7 with locked existing
dependencies. The tested base is the merge above; uncommitted snapshot hashes below
identify actual inputs rather than implying checks ran on a later commit.

| Input snapshot | Actual check and outcome |
| --- | --- |
| Initial authority-first patch SHA-256 `20e0d0896c9d1462d3af9831432ebda2d05aceaef40dfec747d2de2e1e20cbb0` | The six-file focused selection with `-x` failed after 61 passes in 10.64 s at `test_stalled_reopen_helper_does_not_prevent_parent_exit_and_later_recovery[drained-identity]`: its descriptor equality found one retained observer descriptor. This remains a failed execution. |
| Local observer-disposal patch SHA-256 `23d05e6e7eabb9d8891d9806461a1ccdd34ced1f775301e93d5b77ee9d1d4044` | The five-consumer selection with `-x` passed 56 cases in 57.05 s after registering the returned observer's disposal in the inner scope before the first wait. The descriptor assertion was preserved. |
| Shared teardown, native witness and actual-budget source patch SHA-256 `501e55e4f7cb1ac1f06b81380f5fb3d926623234270833fdf4bf8b5adc946b28` | The targeted changed-input selection below passed all 43 cases in 35.33 s. Earlier consumer/core passes remain intermediate evidence because relevant shared inputs changed. Full supported-platform qualification belongs to final-head CI, not these local checks. |

The initial six-file command was:

```sh
uv run --no-sync pytest -q -x tests/conformance/test_recovered_process_lifetime.py tests/conformance/test_lifetime_parent_exit.py tests/conformance/test_lifetime_recovery_claim.py tests/conformance/test_lifetime_journal_recovery.py tests/conformance/test_lifetime_resume.py tests/conformance/test_parent_lifetime.py
```

The consumer repair command used the same selection without
`test_recovered_process_lifetime.py`. Before/after process listings filtered to this
checkout's recovery probe and native child paths contained no retained matching
processes. These listings complement the in-test native process/group and direct-reap
assertions; they do not replace them or establish a repository-wide process audit.
Every new adverse case takes its descriptor baseline before acquisitions, performs
post-cleanup garbage collection and checks exact equality under warnings-as-errors.

Independent correctness, security and performance reviewers inspected the complete
source patch above in separate read-only contexts and returned passing conclusions.
Their reviewed inputs match the nine source hashes below; no native code, test or
build was executed by the reviewers. Correctness covered original failure/cleanup
ownership and sibling finalizers, security covered authority transfer and composite
control closure, and performance covered bounded report retention, the original
cleanup budget, native witnesses and repeated cancellation. The implementation
coordinator records the actual reviewer provenance and returned conclusions with
the change; this source review does not supply executable or CI evidence.

The targeted command was:

```sh
uv run --no-sync pytest -q -x \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_domain_handshake_failure_releases_acquired_resources \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_domain_handshake_cancellation_releases_resources \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_domain_repeated_cancellation_joins_owned_cleanup \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_nonchild_signal_requires_live_leader_and_actual_reaper \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_leader_reaped_with_omitted_live_member_never_signals \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_settle_waits_for_reap_of_exited_leader \
  tests/conformance/test_recovered_process_lifetime.py::test_recovered_late_member_refresh_is_independent_of_live_leader \
  tests/conformance/test_lifetime_journal_recovery.py::test_live_probe_corrupt_journal_releases_acquired_processes \
  tests/conformance/test_lifetime_parent_exit.py::test_live_probe_corrupt_journal_releases_acquired_processes \
  tests/conformance/test_lifetime_recovery_claim.py::test_live_probe_corrupt_journal_releases_acquired_processes \
  tests/conformance/test_lifetime_journal_recovery.py::test_live_probe_handoff_failure_releases_acquired_processes \
  tests/conformance/test_lifetime_journal_recovery.py::test_live_probe_repeated_cancellation_joins_owned_cleanup \
  tests/conformance/test_lifetime_journal_recovery.py::test_recovery_control_capacity_keeps_entire_older_inventory \
  tests/conformance/test_lifetime_journal_recovery.py::test_close_report_never_restarts_expired_process_observation \
  tests/conformance/test_lifetime_journal_recovery.py::test_recovery_close_preserves_complete_inventory \
  tests/conformance/test_lifetime_resume.py::test_completed_unknown_resume_disposes_volatile_descriptors_after_gc \
  tests/conformance/test_parent_lifetime.py::test_conformance_fixture_resources_are_released_after_dead_owner_recovery \
  tests/conformance/test_lifetime_parent_exit.py::test_stalled_reopen_helper_does_not_prevent_parent_exit_and_later_recovery \
  tests/conformance/test_lifetime_recovery_claim.py::test_recovery_claim_excludes_unpublished_and_crashed_parent_writers
```

| Full local check on the same source inputs | Actual result |
| --- | --- |
| `uv run --no-sync pytest -q tests/architecture` | 907 passed in 30.32 s. |
| `uv run --no-sync ruff check --ignore-noqa .` | Passed. |
| `uv run --no-sync ruff format --check .` | Passed; 690 Python files already formatted. |
| `uv run --no-sync pyright` | Zero errors and warnings. |

| Source path under `tests/` | Reviewed and locally tested SHA-256 |
| --- | --- |
| `conformance/lifetime_recovery_probe.py` | `d1801a9b2e4abb3a58d1114e4ee039f35d924ca62064a8e9c18ba368a838d7ed` |
| `conformance/lifetime_support.py` | `fc1977b8288605a766ffcf9deb56e81831c6c3898173308b32f8ce7ee8de8035` |
| `conformance/test_recovered_process_lifetime.py` | `7fa082ee45b958c6a549e14b1d84b9f0211a5a7b2fb69d3c11b7f8bf1db83523` |
| `support/recovered_process_child.py` | `c0ab53c63bb7da4e82d0df282ae79fe07523e99a3855b4dd80dc0d245a5de14e` |
| `conformance/test_lifetime_parent_exit.py` | `188c8985793b60deb00c5dbc1e71c7acdf224d5a0736e58ab839f554e4f1456d` |
| `conformance/test_lifetime_recovery_claim.py` | `3784f65793b9067c120725c40e59a549a79d25b3f5597b94c1a221e16bc5a9bb` |
| `conformance/test_lifetime_journal_recovery.py` | `6d99dd10ae8d06a2385d47751e8f93968f16595fbf06154b77e898ebe10b7a25` |
| `conformance/test_lifetime_resume.py` | `785451e11cc92a05f2b7c9b75fe7024e1159e8779b616a1006d06fc96bab72c6` |
| `conformance/test_parent_lifetime.py` | `50f4ccfc4091b5291cb41f5ba826505fa8239e631ef3a4011f469f310118ed5a` |

The pull request closing [issue #368](https://github.com/realkarych/kora/issues/368)
records the final committed revision, reviewer provenance, required CI links and
supported-platform qualification. Local validation does not claim that final-head
CI has already completed. One complete final-head CI qualifies the full nine-file
integration; no repeated file/CI campaign or extra release qualification is introduced.
Durations remain diagnostics, while actual closure, exact descriptors and hard
deadlines remain gates.
