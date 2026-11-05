# Bounded parallel preparation in the recovery-pair fixture

Checked on 2026-11-05 for issue #767, from base
`57da3c1fef1c4348c1f2889f84da9b81d5ffaca6`. Executable source and the
complete focused local run used `e523a4b49ead8fb398bdb1e23b0bed674336f72c`.
Only three test/fixture files changed: 175 additions and 45 deletions, with
zero production, protocol, configuration or dependency changes. This record
changes no executable input.

## Original failure and bounded hypothesis

[PR #764 CI 42062553408](https://github.com/realkarych/kora/actions/runs/42062553408),
attempt 1, failed the second macOS conformance group's partial-handoff readiness
wait. Its first group passed all 1,064 cases. The second group stopped after
235 passes and one failure; five dependent evidence/aggregate jobs also failed.
This was not a hard job timeout or a first-group lifetime failure.

The five-second initial wait in
`test_live_probe_handoff_failure_releases_acquired_processes[partial]` expired.
Retained child stages span 5.108007542 seconds from entry to report_ready.
The helper exited zero and ran its internal native PID/group/inventory cleanup
assertions. That cleanup does not replace timely readiness. The original host
scheduling/I/O cause remains unattributed, and the execution remains failed.
No unchanged workflow retry or raised deadline qualifies it.

The fixture previously prepared two independent sibling process domains through
serial reserve/acquire/acknowledge chains. The bounded hypothesis is that
separately admitted concurrent direct operations can remove this sequential
fixture dependency while retaining every resource and cleanup obligation.
MutationOwner still serializes journal/tree publication, so the change does not
parallelize durable writes, demonstrate a measured speedup or guarantee startup
within five seconds under arbitrary host load.

## Ownership and behavior

The same already-acquired tree precedes two sibling process preparations under
one TaskGroup. Each existing direct operation retains its own admission slot
and parent-owned operation task. The two-task group is below the unchanged
admission limit of sixteen. Both acquisitions and acknowledgements complete
before constructing the unchanged bounded report. Pair consumers validate
identities, not sibling completion order. Other modes retain their behavior.
No process, resource, durable write or ownership assertion is removed.

Cancelling a preparation waiter does not cancel ParentLifetime's owned work.
The TaskGroup joins waiters before reporting or leaving preparation; invocation
close separately fences resources and joins operations/helpers before releasing
the tree. No new process owner, principal, capability, report format or
production interface is introduced. Original operation, initial-readiness,
cleanup and CI deadlines remain unchanged.

The adverse phase waits for one actual acquisition and holds that sibling
before acknowledgement. A second otherwise model-valid ProcessLaunch supplies
empty argv; the real native gate rejects it during preparation. A nonexistent
executable would be unsuitable because execution follows activation. Only the
expected acquisition-failed reason is converted to the dedicated fixture
sentinel; unrelated lifetime errors or group failures propagate. The group
cancels and joins the held sibling and transfers no report.

Cleanup captures registrations from every native process handle, including
failed acquisition records whose journal entry and task result have no returned
registration. It then requires actual owner closure, PID absence, empty process
groups and zero retained processes. Expected registration cardinality is checked
after closure, so an assertion cannot bypass shutdown. The adverse case checks
both registered handles, including the refused gate child.

HandoffProgress observes only a received-byte count at the existing descriptor
read boundary. It retains no payload or history. A normal report provides a
positive counter check; the adverse phase requires zero bytes, helper exit zero,
actual helper absence, descriptor restoration and fixed refusal stage evidence.
The adverse case uses the existing thirty-second report-read deadline and
five-second cleanup deadline. It does not exercise the five-second report-ready
event or prove cancellation inside a lifetime acknowledgement operation. The
existing partial/cancel-before-ack cases retain that initial five-second wait.

## Local execution and review

On macOS arm64 / Python 3.11.17, the following exact-head command passed with
exit zero:

```sh
uv run --no-sync pytest -q \
  tests/conformance/test_lifetime_journal_recovery.py \
  tests/conformance/test_lifetime_rpc_concurrency.py \
  tests/conformance/test_lifetime_cleanup_controls.py \
  tests/conformance/test_lifetime_admission.py tests/architecture \
  --junitxml=/tmp/kora-767-local.xml
```

All 981 tests passed with zero failures/errors/skips: 930 architecture and 51
focused native/concurrency/cleanup cases. Captured wall output was 93.83 seconds;
JUnit suite time was 93.417 seconds. These are diagnostics on this host, not a
performance comparison with the earlier CI runner. JUnit embeds no Git/command
identity; association with the stated revision/command/environment is
coordinator-recorded. An earlier seven-case authoring subset passed before the
final explicit local Future annotation; the complete run covers final source.

Scoped Ruff format/check with ignore-noqa, strict Pyright and diff whitespace
passed on the same three files. Initial authoring lint required selective
re-raising instead of asserting on a caught error; no rule was suppressed or
weakened. Cleanup-before-cardinality ordering was preserved before tests ran.

Three fresh independent read-only profiles reviewed the nontrivial design and
complete three-file executable diff: correctness, security and performance
passed without findings. Each inspected actual local JUnit and the surrounding
lifetime/native ownership paths. No reviewer executed candidate code, installed,
edited, published or delegated. Reviewer provenance, evidence-assertion review
and complete final-head supported-platform CI remain with the implementing issue
and PR; local/source conclusions do not supply missing CI qualification.

## Limits and follow-up

This is test-fixture preparation, not a production lifetime or runtime change.
Existing TM-06/TM-07 cover fixed diagnostics, TM-09/TM-11 ownership and no partial
handoff, TM-10 bounded admission and TM-12 unchanged evidence/gates. TM-05's
cooperative same-user limitation remains. Existing threat-model entries suffice;
no authority or threat-model edit is introduced. Fixed stderr stages remain the
only probe diagnostic owner; production logging is unchanged.

All five general groups, twelve lanes, platforms, metadata-database coverage,
real resources, workloads and hard checks remain. The first-group lifetime stop
condition remains active. The original delay is not attributed to contention,
filesystem or import time by this change. An unexplained failure cannot be
retried until green or relabeled. Two unsuccessful repairs of one problem still
require an owner decision.

After actual green CI and merge, dependent #764 needs its own final-head CI on
the changed fixture input. Original Codex native measurements remain tied to
unchanged native producer inputs; this fixture does not replace their coverage.
Available master mutation evidence is partial pricing/CEL at
`6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`; the later run at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before execution. It supplies
no applicable recovery-fixture finding or additional PR campaign.
