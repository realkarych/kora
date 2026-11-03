# Recorded-state summaries for failed storage recovery — 2026-11-02

## Scope and accepted boundary

[Issue #634](https://github.com/realkarych/kora/issues/634) implements the [owner-approved diagnostic slice](https://github.com/realkarych/kora/issues/302#issuecomment-6576306000) after the [natural native-release recurrence](https://github.com/realkarych/kora/issues/302#issuecomment-6575367789). The [accepted plan](https://github.com/realkarych/kora/issues/634#issuecomment-6576311594) and [independent design results](https://github.com/realkarych/kora/issues/302#issuecomment-6576078802) define the content and verification boundary.

A failed recovery adds a JSON exception note before the test's later finally cleanup. [Issue #650](https://github.com/realkarych/kora/issues/650) extends the same observation to four existing failed controller-phase assertions: two provider-readiness checks, the attachment-response acknowledgement and the pending-manifest-write signal. Each keeps its exact phase comparison and existing report/stderr diagnostic message; successful comparisons construct no diagnostics. The added note is an immutable string carried by the existing pytest failure/JUnit path. It reports only already-recorded state and private-stream metadata. It does not run another recovery, inspect native processes, identify a dissenter, alter release authority or establish physical closure. Successful recovery does not construct a summary.

The first error retains its own note when later cleanup changes records. Existing exception ordering is preserved: a later cleanup failure may become the top-level error with the first error as context. Expected file/validation failures yield fixed state tokens; unexpected failures in the added summary preserve the original recovery or phase assertion as context. No broad exception suppression is introduced.

## Exact additional output

The fixed input names are `pressure-manifest.json`, `report.json`, `command-stdout.txt` and `command-stderr.txt`. The last two receive metadata inspection only. No private stream content is opened by the summary. The complete output vocabulary is:

| Output keys | Source and allowed values |
| --- | --- |
| `summary_version` | Literal `1` |
| `manifest_state`, `report_state`, `stdout_state`, `stderr_state` | Fixed tokens `ok`, `absent`, `unavailable`, `unsafe`, `oversized`, `invalid`, `changed` |
| `manifest_version` | Explicitly persisted `1`, otherwise `unknown` |
| `recorded_image_requested`, `recorded_attachment_requested`, `recorded_mount_requested`, `recorded_command_inflight`, `recorded_detached`, `recorded_manifest_resources_closed` | Corresponding explicitly persisted manifest booleans; otherwise `unknown` |
| `controller_record_present`, `child_record_present`, `command_record_present`, `image_stamp_record_present` | Explicit valid source record -> `true`; explicit permitted null -> `false`; absent/invalid -> `unknown`. No identity values are emitted. |
| `recorded_command_exit_status` | Explicit persisted integer 0–255, explicit null -> `none`, otherwise `unknown` |
| `recorded_provider_started`, `recorded_cleanup_completed`, `recorded_report_resources_closed` | Corresponding explicitly persisted report booleans; otherwise `unknown` |
| `report_failure_present` | Explicit non-null report failure -> `true`; explicit null -> `false`; absent/invalid -> `unknown`. The string value is never emitted, even when the model accepts it. |
| `stdout_bytes`, `stderr_bytes` | Validated regular-file metadata size 0–1,048,576; otherwise `unknown`. Stream contents are never opened or copied. |

Field presence is taken from explicitly supplied fields in validated records. Model defaults cannot turn omitted fields into observed false, null or closed states. Supplied versions must be integer `1`; booleans and floating-point values cannot be normalized into recorded version evidence. Whole-record validation failure produces `invalid` and unknown dependent fields. Arbitrary report failure strings, process identity strings, paths and unknown fields are not projected. Record presence and recorded flags are not an identity audit, command-settlement proof, atomic snapshot across files or physical-resource inventory.

Each manifest/report read is bounded to 64 KiB. Reads are anchored through no-follow directory descriptors and restricted to regular same-owner files; metadata identity changes are refused. Stream metadata size is admitted only up to 1 MiB. Every descriptor closes before later cleanup. The added serialized note is limited to 8 KiB. These are deterministic input/output bounds, not performance measurements or a new native command deadline.

TM-06/TM-07 cover the added retained content; TM-08 covers truthful state and failure preservation; TM-12 covers unchanged required verification. Content protection comes from the exact scalar projection, not just size limits. Existing bounded native-stderr rendering remains unchanged: the content-free guarantee applies to the additional note, not every historical failure message. Ownership checks do not establish containment of hostile same-user processes.

## Verification and remaining limits

The focused file-boundary regressions cover accepted free-form payload markers, private streams, malformed and unknown fields, omitted versus explicit false/null values, oversized files, symlinks including directory ancestors, nonregular files, changed identity, ownership checks, exact input bounds, descriptor closure and immutable notes across later successful or failed cleanup. Forbidden markers are checked in both returned notes and rendered exception context.

The phase-trigger extension uses the actual assertion helper with real files to retain the original recorded acquisition, command-exit and closure fields after later records change. Both successful and failed later cleanup preserve the first assertion and note; exact descriptor balance and exclusion of manifest-identity/stdout markers are checked for the additional note. Existing raw report/stderr messages retain their own behavior and are not covered by that note's restricted-content claim. The projector, its vocabulary, readers and bounds are unchanged.

New projection tests live outside `test_pressure_controller.py`'s exactly pinned qualification inventory. The existing 17 pressure owners, physical workloads, native commands, recovery/cleanup state machine, private capture, CI matrix, receipts, statuses, deadlines and final-head requirements remain unchanged. The existing failed-native-command test supplies real command/refusal/persistence/closure integration evidence; complete final-head CI supplies supported-platform storage recovery evidence. The implementing PR records actual tested revisions, commands, native results and review input identities.

The original CI41429979660 storage failure remains failed. Its unmounted volume, bound device and zero visible same-UID holders do not establish a cause. This summary adds persisted-state observations; it neither explains nor repairs the native eject refusal. A further natural refusal may still leave attribution unknown. New probes, raw-output publication, release/ownership/privilege changes or acceptance changes require an owner decision; no unchanged rerun is authorized. Parent #302 and its historical unfinished acceptance remain separate.

[CI41644639061](https://github.com/realkarych/kora/actions/runs/41644639061/job/125947551750) later failed during native image creation before provider readiness, with `Resource busy`. Its artifact retained a report and command stderr, but no original acquisition/command manifest or validated final ownership inventory. The report's recorded closure flag does not independently establish physical closure or a leak. The #650 extension preserves what the original controller explicitly recorded before later recovery changes those records; it does not retrospectively fill the missing evidence, identify the native create cause or establish stability. The bounded causal inspection remains stopped at that evidence gap.
