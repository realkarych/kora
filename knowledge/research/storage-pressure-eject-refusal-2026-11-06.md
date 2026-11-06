# macOS storage pressure eject refusal

Date checked: 2026-11-06. Implementation issue: [#886](https://github.com/realkarych/kora/issues/886), a child of [#302](https://github.com/realkarych/kora/issues/302). The [implementation plan](https://github.com/realkarych/kora/issues/886#issuecomment-6692154183) records the approved boundaries. This is test-fixture evidence. It is not a cause-specific fix and does not lower the failure rate.

## Failures

The 120 most recent failed runs from 2026-10-26 to 2026-11-06 contain 14 failed macOS storage jobs. The API shows only the latest attempt, so this is a lower bound.

| Class in retained JUnit | Jobs |
|---|---|
| Whole-device release refusal | 9 |
| `hdiutil create` Resource busy | 3 |
| Pre-diagnostic `command_failed` at acquisition | 2 |

| Failing test | Jobs |
|---|---|
| Deadline | 5 |
| Child failure | 4 |
| Cancelled | 2 |
| Legacy source | 1 |
| Blocker cleanup | 1 |
| Interrupted manifest | 1 |

Run 42228042213 on PR #862 holds the most exact record: phase `eject`, completed, CF closed, status 49168, after a confirmed successful unmount and a fresh unchanged binding. 49168 is `unix_err(EBUSY)` by `mach/error.h`. The holder observation shows zero same-UID holders; 221 other-UID processes were not inspectable.

The `hdiutil create` failures occur before any mount, so the same busy family appears with no kora volume content. `hdiutil create` does not state which internal step refused. This correlation is unproven, and no process is attributed.

## Apple source support

Sources are apple-oss-distributions `main`: DiskArbitration-535.0.10 and IOStorageFamily-337.100.1. They are not version-matched to the hosted macOS 15 binary, so they support a reading and do not prove it.

- `diskarbitrationd/DARequest.c` `__DARequestEjectEject` opens the raw whole disk and issues `DKIOCEJECT`. A nonzero status with no approval dissenter becomes `DADissenterCreate(unix_err(status))`.
- `IOMediaBSDClient.cpp` returns EBUSY for `DKIOCEJECT` unless `getOpenCountForAnchorID(anchor) == 1` and `driver->open` succeeds. Block and raw opens both count. `IOBlockStorageDriver::decommissionMedia` returns busy while the media is an open driver client. `dkopen` failing in the daemon's own open also gives EBUSY.
- The daemon serialises its own commands through `kDAUnitStateCommandActive` (`DARequest.c:369`, `DAStage.c:695`).
- On refusal `__DARequestEjectGetProcessID` runs as root `proc_listpidspath` over raw nodes only and sets the PID only for a match. `diskarbitrationd/DADissenter.c` sets no default PID and the getter returns 0 for an absent key, so no match reads as PID 0. The dictionary reaches the client whole (`DASession.c:248`; eject link path `DAQueue.c:912-1008`).
- Block-node holders are invisible to that lookup. Slice lookups pass an uninitialised `flags` (`DARequest.c:1802`), so a slice match can name a process with no hold on this disk. Only the first match is returned, after the ioctl. Client-side dissenters carry the helper's own PID (`DiskArbitration.c:939`, `:1289`), and approval dissents carry another client's PID.
- `DADissenterGetProcessID` is declared only in `DiskArbitrationPrivate.h` and is exported in the SDK `DiskArbitration.tbd`.

No kora path opens `/dev/disk*` or `/dev/rdisk*` except the intentional test blocker, and a successful unmount excludes volume holds. `pressure_workload` has no subprocesses. Closed-source `hdiutil` helpers are covered only by the same-UID zero-holder observation after refusal.

`test_refused_eject_records_native_dissenter` proves on macOS that a real holder on the raw whole disk produces phase `eject`, code 49168 and that holder's PID and comm, and that a real holder on the block node produces phase `eject` and code 49168. One direct local run on Darwin 25.2.0 also observed comm `python3.11` for the raw holder and no reported process for the block holder; the test does not assert the absence, because a foreign holder can be reported.

## Decision

The refusal is not an owned-resource defect: no owned opener and no release-sequence defect exists on the traced path. It is also not native behaviour that the test may tolerate. `resources_closed=false` is the true report and the image stays attached. A real device-node holder reaches the same kernel EBUSY, so an exception for an eject refusal with zero visible holders would blind the fixture to the leak it exists to detect.

The flake stays an unattributed foreign hold, and no cause-specific repair is supported yet. The fixture now records the process DiskArbitration reports through the helper's native call. This supersedes the earlier #302 owner decision to obtain the dissenter PID from a `diskutil` release: #753 already replaced `diskutil`, and retained `diskutil` refusals showed only "Volume failed to eject".

## Reading the next recurrence

A recorded value is a lead, never proof and never authority. Each row needs its own owner decision. The first matching row wins.

| Phase | Record | Reading | Next step |
|---|---|---|---|
| unmount | code 49168 | Volume lookup (`DARequest.c:1725`). Any unmount failure is forced to EBUSY (`:1525`), so 49168 here says nothing about a device hold. A value means a process with something open on the volume, first match only. | Correlate with holder lines |
| unmount | other code | Local or approval refusal | Inspect that refusal |
| eject | code not of the `unix_err(EBUSY)` form | Another ioctl errno, or a local or approval refusal | Inspect that refusal |
| eject | `dissenter_pid` none | No raw-node match at lookup: a transient holder, a block-node holder, an in-kernel client or a failed lookup. Also the value for a retained record older than this change. | Proposal for a bounded next observation; no remedy |
| eject | PID present, comm none | The process is gone or unresolved, and the PID may be stale | Keep as a lead |
| eject | comm is the python or hdiutil family | Possibly fixture-owned; python alone does not identify the fixture | Ownership investigation, correlated with holder lines |
| eject | comm `diskarbitration` | The source says the daemon sets no default, but the binary is not version-matched; this is not attribution | Treat as no match |
| eject | comm in the mds family | Meets the wording of the holder trigger in #302, but may come from an unreliable slice lookup | Owner decides whether to act or wait for corroboration |
| eject | other named service | A lead with the same limit | Remedy proposal specific to it after corroboration |

Any wait or second release is retry-shaped and is not proposed here. It needs an explicit owner decision naming the holder.

## Disclosure

The PID and tokenized comm of any process DiskArbitration reports, including other-UID processes, reach CI logs and JUnit through the native note, including the `junit.xml` and `tests.stdout` in the `scaffold-tests` artifact of the macOS storage job on every pull request and `qualification.xml` in the storage workflow. They also reach the qualification artifact through `native.outcome` in the preserved manifest. Comm is at most 16 characters of `[A-Za-z0-9._+-]`, otherwise `unprintable`. The sensitivity is low, and the project owner accepted it under #886.

## Limits

The comm is read after the run loop and can belong to a reused PID. A concurrent foreign holder can be returned as the first match, which would fail the exact PID assertion of the raw-holder test. That exposure is the flake itself. The new test adds one image cycle to the module, which slightly raises exposure until a remedy lands. The most likely recurrence result is `none`, which narrows the cause without naming it.
