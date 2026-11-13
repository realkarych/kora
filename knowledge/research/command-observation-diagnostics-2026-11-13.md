# Command observation refusal evidence, 2026-11-13

This diagnostic change is [#1475](https://github.com/realkarych/kora/issues/1475), under the owner-approved [plan for #1469](https://github.com/realkarych/kora/issues/1469#issuecomment-6849542972). It preserves the unresolved root-cause question and changes no production runtime, deadline, diagnostic event or retry behavior.

## Observed failure and test gap

Scheduler [CI 43401325206](https://github.com/realkarych/kora/actions/runs/43401325206), attempt 1, tested merge `19f6f026a198ec41e5f636fb3e3a07f93d12a88b`. Its Linux Python 3.11 local-runtime job recorded 743 passing tests and one failure: `test_command_observation_record_fits_the_reader_bound[fits]` expected `CommandObservation` but received `command_unobserved`. The complete workflow failed. No unchanged rerun was requested. The scheduler change did not modify that source or fixture boundary.

The failing case took 6.640 seconds, while `[overflows]` passed in 6.558 seconds. Those times are consistent with incomplete domain stop, but timing does not identify the refusal branch. The saved artifacts contain no stop/capture diagnostics. A Linux census can be incomplete because an observed process is opaque or unstable, independently of a stray child of the test process; no particular census cause is established here.

The overflow case formerly accepted `command_unobserved` and an absent observation record even when unresolved stop prevented sealing/assembly from starting. It now additionally requires `Completion.COMPLETE`. This proves confirmed domain stop only, not capture sealing or a specific capacity-refusal path. The original outcome, count and size assertions remain.

## Bounded diagnostic boundary

The duration case keeps its original elapsed-time sample before any added diagnostic work. Both cases snapshot completion and journal byte size before teardown; no additional journal contents are read. After both owning contexts close, the existing capture finishes with its unchanged 1 MiB capacity and cleanup deadline.

Assertion messages retain the latest 16 selected records, a count of omitted records, and explicitly separate pre-teardown facts from post-teardown diagnostics. Only the existing `runtime.local.workspace`, `runtime.local.command_stop`, `runtime.local.capture` and `runtime.local.command` event names are admitted. Keeping the tail prevents early busy/refused records from displacing later stop/capture/observation evidence. Each event has a fixed outcome allowlist, including successful command `observed`. Numeric fields are only `waited_ms`, and for capture also `retained_bytes` and `observed_bytes`; values must be finite, nonnegative and at most the signed 64-bit maximum. Invalid values become a fixed sentinel. No unknown string, complete record, path, identifier, argv, token, digest, exception text or payload is rendered.

## Reading the evidence

Pre-teardown unresolved completion plus a corresponding unresolved-stop event localizes the refusal to domain closure, without identifying the underlying census obstacle. Confirmed stop with unsealed/write-failed capture points to capture; `journal_unavailable` points to persistence. Confirmed stop and complete captures followed by refusal narrow investigation to assembly/admission, but do not identify record capacity specifically. Events collected after teardown can include teardown work, and missing best-effort events prove neither absence nor success.

Local macOS checks passed all four selected cases in 0.89 seconds: the duration case, both capacity cases and `test_observation_diagnostic_tail_preserves_late_stop_and_observation`. The latter uses the real logger and capture to verify that 20 early refusal records cannot displace the later confirmed-stop and successful-observation records. The no-suppression architecture suite separately passed all 21 cases in 9.60 seconds. Final checks and one ordinary final-revision CI, including the already-required changed-test repetition job, are recorded in the PR. No extra repetition campaign is added. A failure-free run remains inconclusive about #1469's recurring root cause; it is not a runtime repair claim.
