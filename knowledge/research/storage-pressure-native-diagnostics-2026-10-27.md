# Pressure-fixture native diagnostics and blank images, 2026-10-27

## Scope and authority

[Issue #308](https://github.com/realkarych/kora/issues/308) implements the [bounded plan](https://github.com/realkarych/kora/issues/302#issuecomment-6428995922), with [explicit owner approval](https://github.com/realkarych/kora/issues/302#issuecomment-6429546275). It changes test fixtures, not production storage, public errors, operational logging, version-1 pressure manifests, dependencies, platforms, deadlines or CI gates. [Storage release qualification scheduling](storage-qualification-schedule-2026-10-26.md) and mandatory ordinary resource/recovery checks remain intact.

The delivered improvements are visible native failure diagnostics and removal of the known `create -srcfolder` temporary public mount. Neither proves the historical cause fixed. #293 and #302 remain open for their causal, browser and consecutive-normal-run obligations.

## Retained failures and causal limits

The [existing investigation](https://github.com/realkarych/kora/issues/302#issuecomment-6427350372) records intermittent create/recovery failures on macOS. Native stderr was written under pytest temporary storage and lost from ordinary CI evidence. It identified a temporary mount under `/Volumes` during `hdiutil create -srcfolder`, but did not recover historical stderr or identify the system holder. The suspected system-service race remains a hypothesis. Separate CI lanes run on separate hosted VMs; their count is not an established cause.

A further recurrence at source `e0ae60d8043148b8bd7c712ef112c2a3dd86bb38`, [run 40418023665](https://github.com/realkarych/kora/actions/runs/40418023665), failed `test_storage_pressure_controller_closes_on_child_failure` during recovery with `PressureFailure: command_failed`: 1 failed, 152 passed in 127.71s. Its stderr again lacks the native diagnostic. It remains failed. The source update contained the independent bridge-fixture readiness correction of #306; pressure fixture inputs were unchanged. No unchanged CI rerun was requested.

The earlier source `74ee708` run 40414247628 passed every storage lane but failed a bridge fixture. That pass does not explain or repair the pressure failures. No repeated success is reclassified as causal evidence.

## Diagnostic boundary

`pressure_native.command` still caps collected stdout plus stderr at 1 MiB and retains its existing command deadlines and actual process closure. A failed or terminated native command now supplies a fixed command/subcommand label and observed exit status to `PressureFailure`; full argv and environment are not rendered. Unknown executables get a fixed generic label.

Raw stderr remains available in the existing private file, bounded by collection. Exception display uses escaped bytes, including CR/LF, terminal controls and invalid UTF-8, with an explicit truncation marker and a 16 KiB limit after escaping. It cannot inject terminal control characters. The controller's caught-error path retains that bounded message while preserving the same fixed report failure code and phase stdout. It emits only after cleanup, report persistence and lease closure, so a stopped diagnostic reader cannot delay those owned-resource releases. Relevant phase/report assertions retain bounded diagnostics too. The existing pytest/JUnit/log evidence carries the failure; no artifact allowlist or arbitrary temporary-tree upload is added.

This is narrow native fixture-command evidence. It creates no permission to include backend exceptions, private paths, payloads or arbitrary exception state in production logs or public errors.

## Image ownership and recovery

The macOS command is now:

```text
hdiutil create -size 64m -fs HFS+ -volname kora-pressure-<nonce> -o <private-owner>/filesystem.dmg
```

UDRW is verified by actual `imageinfo`, rather than requesting `-format` without its supported source-folder form. A no-follow image descriptor verifies regular type, effective UID, one link, finite size and captured device/inode before `fchmod(0600)`. Its private directory and the path's matching identity are checked; the controller keeps its lease and persists the resulting stamp before attachment.

Attachment retains `-nobrowse -owners on -mountpoint <private-owner>/mounted`. Unique image/device/mount discovery precedes an opened mounted-root check of device, inode, type, UID and filesystem. An owner-owned 0755 or 0700 HFS+ root can be normalized through that same descriptor to 0700 and checked again. Unexpected identity or ownership refuses without privileged ownership repair.

The lost-attachment-response checkpoint still occurs before permission normalization. Recovery discovers and detaches the known attachment even from that checkpoint. Version-1 manifests remain unchanged; an optional validated legacy `volume-root` directory is still removed only by its owner after native detach. Busy workload holders still prevent unmount and retain ownership. There is no force detach, holder exception, indexing change, retry or extended deadline. These are cooperative local ownership checks, not hostile same-user containment.

## Local native observation

Hypothesis: the selected blank-create command formats an unmounted device and omits the specific source-copy public mount. Decision criterion: one bounded `-debug` trace shows no `/Volumes/` path or source-folder copy, while the native operation exits zero, discovery finds no remaining attachment, and the controller closes its owned image.

Environment: macOS 27.0.1 (26A434), arm64, uv CPython 3.11.17. Other local sessions may share load; no elapsed-time or memory guarantee is inferred.

The command above was executed once with `-debug`, using the existing controller's persisted command/image ownership, native process identity and bounded collector. It exited zero, image discovery found no attachment, and owner cleanup completed. The raw stdout is 130 bytes (SHA-256 `c7a1d984f5df4f06b13dfabbca25e7ae0370911893c87de8d6b1be5cdea5991c`); stderr is 34,144 bytes (SHA-256 `67150aa607b0add44cb55f9794d579e2d78167dd681e9400cf366e4c0114b29b`). Diagnostic excerpts:

```text
autodiskmount: false
format: formatDevEntry: returned 0
format: skipping post-newfs mount/unmount because user did not request a journal
-ejectAfterFormat: synchronous unmount returned 0x00000000 (0)
-ejectAfterFormat: synchronous eject returned 0x00000000 (0)
-ejectAfterFormat: returning 0
```

No `/Volumes/` path occurs in either bounded trace. This observation supports removal of this specific public-mount exposure on the inspected tool/platform; it is not a claim that all image operations avoid temporary devices, that before/after inventories prove absence of a transient mount, or that the unknown hosted macOS failure is reproduced. Hosted macOS 15 behavior remains a required CI check.

## Verification boundaries

The real-command regression verifies printable bounded display, invalid/control bytes, oversized stderr, exact raw bytes, persistence failure and closure. The real controller-subprocess regression verifies the caught-error path and closed child identity. Native image creation records the actual delegated arguments, verifies finite private image identity and UDRW, and refuses the source-folder route. A legacy-shaped orphan recovery checks actual detach and image/mount/optional-source-directory removal. Existing ENOSPC, cancellation, deadline, child failure, lost response before chmod, interrupted manifest, failed unmount and image-substitution cases remain blocking.

Initial test-first results were four expected failures: missing rendered command/exit diagnostics, absent configured caught-native-failure workload, and no returned inspected image stamp. After implementation the controller/uncertainty set passed 20 tests; the complete local storage selection passed 157 tests. Strict Ruff and Pyright passed. These are local observations; final committed-revision checks, supported-platform CI and independent implementation reviews are recorded in the PR, and missing evidence is not a pass. No new numerical qualification or performance campaign is introduced.

## Stopped diagnostic reader regression

An initial implementation wrote the caught diagnostic before entering cleanup. A prefilled blocking stderr pipe reproduced the defect: no final report was saved before the existing 30-second phase deadline, because the controller stopped at that write while still owning its child. The failing observation is retained separately from later passes.

The message is now retained until the controller has cleaned up, saved its report and left the lease context. The regression fills a real pipe to nonblocking saturation, restores blocking writes and launches the real controller without draining that pipe. It waits on report existence and successful nonblocking lease acquisition with a deadline, and verifies closed child identity and resource state while the controller is still blocked on diagnostic output. Parent cleanup then terminates and reaps that controller and performs the existing idempotent recovery. No sleep, mock, retry or larger timeout is used. The drained-pipe regression separately verifies diagnostic delivery and raw bytes.

## Qualification inventory reconciliation

Run [40420232374](https://github.com/realkarych/kora/actions/runs/40420232374) at `d74b59a` passed all storage lanes but failed all three general lanes in `test_storage_qualification_cases_remain_in_ordinary_collection`: the qualification inventory still named the old unparameterized direct-command case. The failed run is retained. Its required joins failed correctly; no unchanged retry was requested.

The exact case set now contains both `[1]` and `[4096]` identities and the four new pressure cases. The real ordinary-collection check additionally requires exact coverage of the pressure-controller file, so future added pressure cases cannot be omitted from qualification. The release workflow still executes that entire file and retains exact JUnit equality.

A bounded real execution of the 19 pressure-controller cases, followed by the unchanged recovery collector, verified the corresponding raw counts: 17 distinct manifests, 12 primary reports and 17 final reports, no cleanup failures, and exactly two pre-provider `command_failed` primary reports. The two unsafe-ancestor refusals still create no owner. The additional direct-command parameter plus four new cases add five owners to the original 12; caught failure, stopped-reader cleanup and legacy orphan recovery add three primary reports to the original nine. Every report is closed.

The raw validator and parser fixture now require those exact larger inventories and both new native-failure reports, preserving the original identity, ownership, ENOSPC, failure, numerical and closure assertions. Corruption cases verify that losing either the failure code or the pre-provider classification is rejected; both cases failed when that check was absent and passed after its restoration. No cycle count, capacity limit, budget, schedule, deadline, platform or qualification policy is loosened. No instrumented qualification campaign was rerun. This reconciliation accounts for the approved additional tests and prevents omission at their existing evidence boundary.
