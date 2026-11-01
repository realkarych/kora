# Ordinary retirement failure evidence

Issue [#604](https://github.com/realkarych/kora/issues/604) implements the
[approved diagnostic plan](https://github.com/realkarych/kora/issues/460#issuecomment-6559595183)
for unresolved #460. This changes failure evidence, not runtime behavior.

## Natural observation

[PR #599 CI](https://github.com/realkarych/kora/actions/runs/41316370329/job/124877220302)
tested requested head `cc23bb3cd26f741ffcaa275613f1474423ae49f1` through merge checkout
`1d6d512dc67750ff456cc5787af498bb88987a9c`. The macOS ordinary local-runtime lane
reported 1 failed, 246 passed and 1 teardown error.
`test_local_runtime_releases_resources[starting_retire_is_released]` raised
`runner_stop_unconfirmed` in 0.400 seconds. Its report did not retain the retirement
outcome. Artifact 13086739818 contains the JUnit report, SHA256
`54c09e7e75ead51f843635e67a86ae9dbf7bb5969140431d6277aba9be6d5c12`.

Session cleanup subsequently found a runner. Pytest attached that teardown error
to the last test; this does not establish which scenario created the process.
Neither the common refusal nor the duration identifies the scan boundary, native
cause or a grace timeout. Existing #508 and #561 changes were present. No causal
repair is established by this observation.

## Bounded diagnostic

The single retirement in `starting_retire_is_released` now uses a test-support
context that adds a note only for `runner_stop_unconfirmed`, after the existing
runtime close. It reuses the measurement's safe last-observation parser. The note
contains only the closed outcome, bounded matched/killed counts, finite elapsed
milliseconds and applicable closed scan reason. Missing or invalid observations
remain null; capture failures attach an explicit unverified note and preserve the
capture failure as the cause of the original protocol exception.

There is no additional retirement between this call and capture completion. The
field remains named `last_observed_runtime.local.retire`: it is not execution
authority, confirmation of termination or a proven explanation for #460. Runtime
code, session leak detection, ordinary workload cleanup and deadlines are unchanged.

`test_ordinary_retirement_note_preserves_refusal_and_live_carrier` exercises a real
incomplete-listing refusal with normal and deliberately bounded overflowing
capture. It checks the original exception object, retained fence, live carrier,
closed runtime jobs, safe note and explicit test-owned cleanup. Existing parser
tests retain invalid-last-event, missing-event and forbidden-content coverage.
The successful ordinary retirement remains covered by its existing lifecycle test.

A passing diagnostic run does not resolve the intermittent failure. The next
natural refusal with this note, or a deterministic reproducer, is the trigger for
a separately justified runtime repair. No unchanged rerun is evidence of a fix.
