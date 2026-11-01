# Revalidate an omitted dead recovered leader

Date: 2026-11-01. Implementing issue: [#580](https://github.com/realkarych/kora/issues/580). Governing contracts are [the lifetime ABI](../specs/conformance-lifetime.md), [the accepted observation design](recovered-settlement-observation-2026-10-30.md) and [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md).

## Counterexample and bounded correction

A recovered observation can read a complete matching dead leader snapshot immediately before its reaper collects it. The following native group census is empty. The previous omitted-leader revalidation branch excluded initially dead snapshots, retaining the stale present state and refusing settlement with `ProcessError(UNKNOWN)` even though a fresh absence and empty-domain confirmation were available.

The correction removes only that non-dead predicate. Both live and dead initially valid snapshots can enter the existing omitted-leader recheck when no collection was already established. The branch accepts only fresh absence or a complete matching dead identity/domain. Successful settlement still requires the separate fresh empty-group census, final absent-leader read and retained-watch checks. Unknown, malformed, mismatched or live refreshed observations still refuse. Signal authority, watch ownership, scan capacity, absolute deadlines and the existing refresh policy are unchanged.

The regression `test_recovered_dead_leader_omitted_during_collection_is_revalidated` uses an actual held-reaper child. It captures its complete native live identity/domain, witnesses its real exit through `ExitAnchor`, actually reaps it and injects exactly one delayed dead observation through `IdentityReader` before exclusively native reads and group census. Linux supplies its actual native dead snapshot. On macOS, the test constructs the dead flag from the witnessed exit because the native reader can report an exited PID absent; it does not claim a native Darwin `PRESENT/dead` result or introduce production session inference.

The test requires fresh native absent-leader reads and empty-group censuses, successful release, zero retained watches, no refresh waits and restored descriptors. All helper processes and anchors retain their existing real cleanup owners. No patch, mock, synchronization sleep or weaker assertion is introduced.

## Evidence and limits

On accepted base `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`, whose affected runner source matches `46ce34c`, the named regression failed at `settle()` with terminal `ProcessError(UNKNOWN)` on macOS/Python 3.11.17 before the production edit. JUnit: `/tmp/kora-pr-finish-20261101/recovered-base-red.xml`. This deterministic protocol sequence establishes the counterexample; it does not identify the precise native interleaving of the original failed Linux CI `41258846418` in deadline PR #579. That historical failure remains failed.

The implementing PR records corrected-source checks, independent correctness/security results, final revision and complete Linux/macOS CI. Source inspection alone establishes no executable pass. The latest master mutation audit `41297342266` is inspected for applicable findings without adding a PR mutation campaign or score gate.

No event, public operation, persisted format, dependency, platform, resource owner or numerical acceptance method changes. Existing content-free errors and output ownership remain. This bounded recovery correction adds no hostile-process containment claim or production-consumer activation.
