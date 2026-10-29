# Storage qualification scheduling

Checked 2026-10-26. The owner approved [issue #283's complete plan and exact wording](https://github.com/realkarych/kora/issues/283#issuecomment-6418570450), with [approval recorded](https://github.com/realkarych/kora/issues/283#issuecomment-6418749396). This changes qualification scheduling and per-PR evidence requirements; it does not establish a timeout fix or a new performance guarantee.

## Manual-only storage qualification amendment, 2026-10-29

The project owner approved [issue #426](https://github.com/realkarych/kora/issues/426) to remove only the daily storage cron and retain the complete study as a manual exact-release prerequisite. Unattended daily storage qualification is no longer provided. This supersedes the earlier storage cadence, without changing the separate local-tooling nightly workflow or ordinary PR checks.

[Run 40874097622](https://github.com/realkarych/kora/actions/runs/40874097622) at `e8adaaeac1169c1d0e5c42d3378a18838f7e64e4` remains failed: macOS cohort 0 exceeded 480 seconds and the evidence union failed. [Issue #396](https://github.com/realkarych/kora/issues/396#issuecomment-6497108118) retains its checkpoint, partial report, raw artifacts, source and limitations. This scheduling amendment neither fixes that timeout nor qualifies any release.

## Retained execution failure

[PR #281](https://github.com/realkarych/kora/pull/281), revision `e1be05c2b9f82e2998c36f25dfe94e6d0a63dcc2`, passed ordinary supported-platform tests. [Storage run 40321686291](https://github.com/realkarych/kora/actions/runs/40321686291) failed because [macOS producer 2](https://github.com/realkarych/kora/actions/runs/40321686291/job/121567932318) exceeded its existing 480-second process deadline. Its required evidence union consequently failed. These results remain failed.

| Evidence | Observation |
| --- | --- |
| Failed producer completed rows | 830 of 832: 14 warm-ups, 800 small cycles and 16 large cycles |
| Incomplete measured work | Two final 256 MiB alias cycles |
| Completed gross cycle time | 406.968 seconds |
| Recorded phase/control observation within gross time | 5.424 seconds |
| Last completed alias cycle | 19.929 seconds gross |
| Same-source macOS producer 0 | 832 executed rows, 112 resource points, exit zero; complete duration 365.557 seconds |

The remaining time before termination includes warm-ups, sampling, allocation snapshots, GC, startup and the interrupted operation. It cannot all be attributed to the observer. Completed disposable-resource checkpoints were zero; closure of the interrupted operation is unverified. Different elapsed times do not identify a particular host defect or demonstrate a storage leak. One successful producer does not qualify the failed complete campaign.

The failed [artifact 12650046137](https://github.com/realkarych/kora/actions/runs/40321686291/artifacts/12650046137) has ZIP SHA-256 `9fca7cd4138174460cb844706be080db0c2d4072a2f356c54c4afc5185cb26ec`. The successful [artifact 12649542809](https://github.com/realkarych/kora/actions/runs/40321686291/artifacts/12649542809) has ZIP SHA-256 `cac4598f189c5d87c12fef89656f28ca7263b346b920e93f0208387fdc33950b`. Both record CPython 3.11.17, APFS, the same revision and storage source digest `c58781849e856c6d98623dac4e0b7642faffa52ef27f0f52734e477eeca125ed`.

A bounded local macOS/CPython 3.11.17 diagnostic used unchanged real raw 64 KiB and alias 256 MiB cycles with cProfile and tracemalloc, on a shared host. The alias cycle performed 24,808 executor submissions; expected-fixture digest construction occupied 0.266 seconds under profiling. A separate 16-part digest calculation took 0.090 seconds without tracing. These are exploratory observations, not controlled qualification or causal attribution of the CI timeout. They do not establish that digest caching would be a sufficient fix. Product admission, durability or cancellation behavior was not changed to accelerate the measurement.

## Preserved ordinary checks and explicit reduction

The complete existing storage workflow runs only on manual dispatch before a release under the 2026-10-29 amendment. Its six long-lived producers, two qualification producers, all workload/cycle inventories, source checks, working-memory/physical-storage limits, hard deadlines, raw failure artifacts and failure-preserving union remain. Before releasing local storage, the complete campaign must succeed for the exact release revision. There is no automated release workflow; release admission remains a human obligation. Any dispatched timeout still fails qualification.

Ordinary scaffold storage lanes retain all integrity, replay, cancellation, ownership, pressure, crash and diagnostic-failure tests. Three conditions formerly enforced only by the separate evaluator are also asserted in the ordinary helpers: the existing 979-step online query-work ceiling, exact first/middle/final/restart keysets, and the completed reconstructed stage's identity. Existing evaluator checks remain too.

Full raw pressure/closure, sink and boundary cross-artifact checks and instrumented captured-Python/physical-storage study limits no longer qualify every PR. Ordinary runs retain their assertions and collection/JUnit/log evidence. This is an explicit reduced per-PR evidence guarantee, not unchanged campaign coverage or proof of leak freedom. Other component obligations remain unchanged.

The prior [ordinary CI run 40321686090](https://github.com/realkarych/kora/actions/runs/40321686090), attempt 1, supplies existing coverage evidence for requested revision `e1be05c` and actual merge checkout `3b5ae08e6041fdf4501d72ecdd2e850310b2baad`. Each Linux 3.11, Linux 3.15 and macOS 3.11 storage lane ran 148 tests, including all 38 qualification cases: 14 boundary, six sink, 14 pressure and four crash cases. Collection/JUnit hashes matched the receipts; there were no failures, errors or skips. This prior result is not final-head evidence for the scheduling amendment or its added ordinary assertions.

## Protection and verification authority

Before the amendment, master protection required 16 contexts from app 15368 with strict updating disabled. Owner approval permits removing only `Required storage evidence`; all other context/app associations, review settings, the active linear-history ruleset and CodeQL security rules remain. The implementation reads current settings before the narrow update, preserves concurrent additions and verifies the saved difference. The same named job remains mandatory inside release qualification.

TM-12 covers the changed verification authority. Storage recovery, resource and identity controls under TM-09/TM-10/TM-11 and content protection under TM-06/TM-07 remain unchanged. The threat model's blocking-check wording does not prescribe per-PR scheduling; the development and verification documents specify this accepted split. Independent correctness, security and performance reviewed the plan and require final implementation evidence before readiness. Historical results retain their original source/revision, and the amended ordinary helpers require fresh affected checks; no old artifact is relabeled as an execution of a new revision.
