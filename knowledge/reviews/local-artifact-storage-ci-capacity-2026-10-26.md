# Local artifact storage CI capacity

Historical disposition, condensed 2026-11-01. This record explains the original #255/#258 CI split; it does not describe the current lane layout or qualify present capacity. The [immutable original](https://github.com/realkarych/kora/blob/c407647ee16e4969e934479f1c3a6cb197dd3e95/knowledge/reviews/local-artifact-storage-ci-capacity-2026-10-26.md) retains exact revisions, observations, commands and review-status history.

## Observed failure and enduring coverage rationale

At `6790e4a10360edb6e40c3935833736b983f1a039`, [storage qualification 40242106476](https://github.com/realkarych/kora/actions/runs/40242106476) passed all six producers, 832 rows and 112 resource points per producer, and both 38-test platform qualification jobs. The separate [general workflow 40242106485](https://github.com/realkarych/kora/actions/runs/40242106485) did not qualify: Linux passed 5,408 tests and subsequent steps but its 601-second job was cancelled; macOS was cancelled during tests. Successful steps did not convert cancelled jobs into passing evidence.

The original refinement separated storage and other ordinary checks. Its durable requirement is complete, nonempty, disjoint collection whose union equals the environment's full inventory, with exact checkout/requested revision, run/attempt, environment/database, argument and hash provenance. Successful exits and actual matching JUnit identities are required; missing, overlapping, skipped, cancelled or inconsistent evidence refuses the protected joins. This historical record adds no exemption for a matrix aggregate or receipt without successful execution.

## Current owners and limits

The [approved contributor audit](../research/approved-verification-audit-2026-10-29.md#ci-admission), [current development guide](../../docs/development.md) and [workflow](../../.github/workflows/ci.yml) own current test selection, evidence bounds and lane admission. [Scaffold evidence tests](../../tests/architecture/test_scaffold_ci.py) and [ordinary CI tests](../../tests/architecture/test_ordinary_ci.py) protect that integration. The old six-lane layout, removed test name and pending wording-review note are retained only in the immutable original.

Whole-workflow timing, including queues and dependencies, is diagnostic against the ten-minute feedback target; the accepted [bounded MVP amendment](../research/bounded-mvp-verification-2026-10-29.md) governs merge readiness. Hard job/test/cleanup deadlines, supported platforms/databases, exact resource release and all mandatory final-head checks remain. Historical whole-pipeline acceptance failures retain their original verdicts. Old source checks and storage campaigns supply no current final-head capacity or release-qualification evidence; the [storage schedule](../research/storage-qualification-schedule-2026-10-26.md) retains exact-release obligations.
