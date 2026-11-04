# macOS CI environment qualification history

Updated on 2026-11-05 for issue #720 and PR #721. The macOS 26 experiment
completed; its results do not qualify macOS 15 or establish runner availability.
The approved follow-up restores actual macOS 15 execution and keeps master
protection unchanged.

## Historical evidence

The initial candidate selected macOS 26 for four scaffold lanes, diagnostics
and mutation tooling, with matching aggregate and receipt identities. Linux,
twelve-lane coverage, fixture cleanup and hard deadlines were preserved.

[Run 41903024715](https://github.com/realkarych/kora/actions/runs/41903024715)
failed at head `90f514f9ee74216423405cdb082cb043e0a60096`: the macOS conformance
repeated-cancellation test exceeded its initial five-second readiness wait.
This happened before intentional cancellation assertions. Probe diagnostics
were lost when cleanup consumed the child task's cancellation exception.
The diagnostic repair preserves existing bounded stage and stderr evidence
on the original failure after joining cleanup; it does not fix startup latency.

[Run 41908448721](https://github.com/realkarych/kora/actions/runs/41908448721),
attempt 1, passed at head `219f0bf2d63244eb0cc586c54f17b43b1f3d87a6`.
Its tested checkout was `14fc54e80cfeccb6101fc08cf4291c5e639305a8`, combining
that head with master `213f08c5291c7d6f4430d601c3dd85e8c87f8c5e`.
All twelve scaffold lanes, evidence verification and platform aggregates passed,
including macOS 26 host-fixture preparation and removal. The PR recorded
47 successful checks and one skipped whole-repository dependency scan.
The run lasted 18 minutes 19 seconds including scheduling; hard job and test
deadlines were unchanged. The readiness failure did not recur and its cause
remains unknown. These results are historical macOS 26 evidence only.

Earlier macOS 15 runner non-acquisition in #715 and #717 remains unresolved.
Changing images did not prove access to a different capacity pool or fix those
failed attempts. The separate storage release workflow remained on macOS 15.

## Restored verification contract

The owner-approved [return plan](https://github.com/realkarych/kora/issues/720#issuecomment-6642600007)
restores all six actual macOS executions and the aggregate identity to macOS 15.
The earlier proposal to migrate master protection is withdrawn. The existing
required status remains `Scaffold (macos-15, Python 3.11.17)` with GitHub Actions
app ID 15368; all other required contexts and protection settings remain intact.

The twelve-lane verifier requires actual macOS 15 receipts, matching run,
attempt, requested revision, checkout revision, inventory and integrity.
Renaming a macOS 26 artifact cannot make it admissible. Negative tests reject
both macOS 26 and Ubuntu receipts under an expected macOS 15 artifact path.
The readiness diagnostic repair and its real-process regressions remain intact.

Restored final-head Linux/macOS 15 qualification is pending. It requires one
separately authorized complete run with all required checks, twelve same-attempt
receipts, native host tests and successful cleanup. Missing or cancelled runner
execution remains incomplete evidence. Stop and report a failed or incomplete
attempt instead of retrying unchanged until green; investigate retained probe
evidence if readiness fails. Historical macOS 26 success cannot satisfy this gate.
