# Claim expiry handoff watchdog, 2026-11-26

The project owner approved a 15-second outer watchdog for the full ordered Claim expiry handoff sequence on 2026-11-26. The timer starts at the same point after secondary connection setup and encloses both full database state observations, exact Claim replay, heartbeat, the competing Claim and settlement of the held continuation. The bound applies to this one test across the interrupted and uninterrupted SQLite and PostgreSQL cases. [Issue #1910](https://github.com/realkarych/kora/issues/1910) · [Approved plan](https://github.com/realkarych/kora/issues/1910#issuecomment-7200433315) · [Owner approval](https://github.com/realkarych/kora/issues/1910#issuecomment-7200459641)

## Hosted observations at ten seconds

The assigned Ubuntu 24.04 / CPython 3.11.17 `general-5` lane in [PR #1906 CI attempt 1](https://github.com/realkarych/kora/actions/runs/45867668585/attempts/1) failed at requested head `5bc29aea432daa30cea01699fc9bcf6daab75b23` and hosted checkout `799f7e96933e27eb5d0be4b72f4a169301e646d2`. The lane's test step exited 1 after `test_run_latch_denies_competing_claim_and_live_heartbeat_before_cascade[sqlite-False]` raised the fixture's outer `TimeoutError` while waiting for the competing Claim. This was a direct Kora test failure. [Failed lane](https://github.com/realkarych/kora/actions/runs/45867668585/job/140080573282) · [Failed JUnit archive](https://github.com/realkarych/kora/actions/runs/45867668585/artifacts/15144320828)

The failed case appears in `child-1.xml`, SHA-256 `bc8f7aec4523ca3a91e8a75ca8eef2a5662a373c2a80d94ebe5b5bcc0327a87d`. The archive SHA-256 is `83e1d97acd200365a5f8d766562cd9dc2ec5379b023b99a1629998f356371161`. The JUnit time of 20.754 seconds includes work outside the watchdog. Its exception note records the remaining milliseconds at each phase.

| Observed phase | Milliseconds remaining |
| --- | ---: |
| Watchdog entry | 10,000 |
| First full snapshot returned | 7,549 |
| Replay returned | 4,766 |
| Second full snapshot returned | 4,758 |
| Heartbeat returned and competing Claim entered | 3,109 |
| Competing Claim's SQLite writer checkout | 918 |
| Competing Claim's raw `BEGIN IMMEDIATE` returned | 915 |
| Release of the held continuation during timeout unwinding | −7 |

Connection setup had already moved outside the watchdog under [issue #1866](https://github.com/realkarych/kora/issues/1866) and [PR #1867](https://github.com/realkarych/kora/pull/1867). The two full state observations took 30 ms combined. The competing Claim spent 2,191 ms before its recorded writer checkout. The raw begin return followed 3 ms after checkout. The trace observes successful begin return, and the exact start of the native statement is unobserved. It gives no attribution inside the Claim or heartbeat operations and no general bound for native contention. The bounded failure note continues to distinguish the outer watchdog from nested timeouts.

The [Ubuntu 24.04 / CPython 3.11.17 master `general-5` lane](https://github.com/realkarych/kora/actions/runs/45866249672/job/140074410555) passed on requested and checked-out revision `d7a899b8ef4a71ef9895c63850483acc05a6fb1c`, attempt 1, with SQLite and PostgreSQL selected. Its receipt binds `child-2.xml` SHA-256 `6d6931edb8156a3e43fc9e526214e79cdafbdba6a787040dc61a37f6af365fb6`. The [passing JUnit archive](https://github.com/realkarych/kora/actions/runs/45866249672/artifacts/15141429662) has SHA-256 `e3ca0aa3a6879885f21049dfb52e59451e6b8c9250efa2fbbd3681e804f7d568`. All four variants of `test_run_latch_denies_competing_claim_and_live_heartbeat_before_cascade` passed in that child.

| JUnit parameter | Result | Whole-case seconds |
| --- | --- | ---: |
| `sqlite-False` | Passed | 11.774 |
| `sqlite-True` | Passed | 9.054 |
| `postgresql-False` | Passed | 9.309 |
| `postgresql-True` | Passed | 8.493 |

The checkouts have identical Python engine and database source, related tests and database fixture inputs, `uv.lock` and CI workflow. The TypeScript dependency lock and placement of the handoff test among lane children differ. These executions provide separate observations under the ten-second cutoff. They provide no matched performance comparison or evidence that 15 seconds covers every hosted execution.

## Retained behavior and verification

The [Worker Claim contract](../specs/api-and-agent-surface.md#worker-claim-activation-1450), [inherited deadline enforcement](../specs/scheduler.md#17-inherited-deadline-enforcement-adr-0022) and [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md) govern the behavior exercised. The fixture uses exact authority-clock expiry and rollback, real database operations, and explicit synchronization at the native SQLite checkout and PostgreSQL advisory-lock boundary. Those mechanisms establish the operation order and the contention exercised. The two full snapshots still prove unchanged state across byte-identical replay, the recovery-only heartbeat and rival Claim denial still require the original cascade to be paused, and settlement still requires the persisted final effects and both database owners to report zero in-flight units. The other cases in the owning module continue to check real rollback, native refusal, cancellation and release. Production deadlines and transaction bounds continue to govern actual operations.

The 15-second watchdog remains a blocking fixture containment bound. A timeout fails the case and keeps the existing bounded phase and SQLite writer note. The local owning-module run exercises both databases, and the final GitHub CI run supplies hosted evidence, including changed-test repetition and ordinary scaffold checks. If a qualified execution exhausts the 15 seconds, retain the failure and its existing trace for an owner decision. The approval covers this cutoff for this fixture only.
