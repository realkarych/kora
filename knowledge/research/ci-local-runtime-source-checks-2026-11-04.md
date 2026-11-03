# Place source checks before local-runtime fixture acquisition

[Issue #680](https://github.com/realkarych/kora/issues/680) implements the [reviewed bounded proposal](https://github.com/realkarych/kora/issues/328#issuecomment-6617839258), with [owner approval](https://github.com/realkarych/kora/issues/328#issuecomment-6617873371) on 2026-11-04 for placement and exact development-guide wording. It revises the source-check owner from [#586](ci-source-placement-2026-11-01.md), without changing offline distribution placement or test membership.

## Failure and diagnostic basis

PR #676 head `8debc18c7f309a9fbf2854903e5d85f93dde439e`, [CI41733577943 attempt1](https://github.com/realkarych/kora/actions/runs/41733577943), cancelled macOS conformance job126256941813 at its hard ten-minute maximum. Pre-test phases took151seconds, including103seconds of source/frontend checks. Tests ran22:49:16–22:56:54 UTC before cancellation; the artifact retains complete collection with exit0, but both group logs stop around98% without group JUnit or exits. No individual test failure or completed cohort is established. The cancellation and five dependent failed joins remain unqualified.

The same run's local-runtime jobs passed in92/121/77seconds on Linux/Python3.11, Linux/Python3.15 and macOS/Python3.11. Those rows now take the serial source block before acquiring fixtures. Additional pinned frontend bootstrap changes their process history, installed files/cache and build products. Natural durations support considering the move; they do not establish a calibrated timing prediction, bounded heap result or future deadline guarantee. The source step is removed from conformance without changing its two concurrent groups.

The whole failed workflow took16m46s including queues,406seconds over the diagnostic feedback target. GitHub also reported macOS runner capacity constraints; scheduling and the execution timeout are distinct. #527/#328 retain feedback/headroom maintenance. Storage Resource busy remains a separate unattributed native issue; this placement does not repair it.

## Required behavior and ownership

All twelve jobs install the existing locked frontend toolchain/dependencies. On all three platform/interpreter rows the unchanged source command block runs only in local-runtime, serially before Linux collision checks and host-fixture preparation. Its commands remain unprivileged; no credential, permission, destination, package/version or cleanup authority is added. Normal failure conditions prevent acquisition after unsuccessful frontend/static work. Existing always-running teardown acts only on its validated ownership marker and retains all release checks.

Local-runtime completion now requires frontend and static success plus the existing fixture lifecycle. Conformance requires frontend success and static skipped. Remainder keeps offline distributions, and every lane retains tests, upload, final sync and cancellation checks. Failed, skipped, missing or cancelled required steps cannot qualify a lane or downstream shared evidence/Scaffold/CEL joins. A successful JUnit cannot replace a missing step or job result.

The twelve-row matrix, five general groups, exact selected tests, child launch/join behavior, isolated temporary/cache directories, logs/exit/JUnit records, complete/disjoint inventories, receipts, action/dependency pins, platforms/databases, permissions and every hard job/test/cleanup deadline are unchanged. Source before host acquisition changes process history; it does not make unrelated observers, privileges or extra concurrent work acceptable.

## Verification and stop conditions

Architecture checks pin all-row frontend bootstrap, exact source commands and placement before host acquisition. Existing real extracted-shell tests exercise each incomplete outcome under the updated lane ownership. Existing inventory, receipt, join, fixture lifecycle and no-suppression checks retain their obligations without a duplicate evidence framework. Independent correctness/security/performance actual-diff review and one complete successful final-head CI are required; the implementing PR records exact revisions, commands, results and timings. No additional native-review checkpoint or benchmark campaign follows from this move.

The [inherited first-group lifetime stop](ordinary-ci-cross-lane-placement-2026-11-02.md) is stricter than ordinary repair iteration: any lifetime-guard failure attributed to, or unresolved under, changed contention/process history returns to the owner before repair or another run. The ordinary two-unsuccessful-repair rule does not override it. No unchanged second CI attempt, gate relaxation or further placement is authorized. Existing TM-08/TM-12 evidence/source controls and local-runtime resource ownership remain active. No executable product component or diagnostic sink changes.
