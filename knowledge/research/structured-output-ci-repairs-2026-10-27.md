# StructuredOutput delivery CI repairs

Historical disposition, condensed 2026-11-01. The [immutable original](https://github.com/realkarych/kora/blob/c407647ee16e4969e934479f1c3a6cb197dd3e95/knowledge/research/structured-output-ci-repairs-2026-10-27.md) retains issue #246's approved repair plan, reproduction evidence and original scope limits. These were cleanup-fixture and browser-installation repairs, not a StructuredOutput contract amendment.

## Descriptor ownership repair

[CI 40414018517](https://github.com/realkarych/kora/actions/runs/40414018517) failed a cancelled storage-reconstruction case with `effect_unsettled`. Bootstrap could receive three SCM_RIGHTS descriptors before the helper exited, while test teardown closed the helper before `MutationOwner.bootstrap()` adopted the descriptors.

The finalizer uses existing `dispose_settled()`: pending mutations or unclosed helpers still refuse; otherwise unadopted descriptors close before strict release. The regression requires actual process reaping, closed channel/anchor, all three invalid descriptors, zero retained work/cleanup resources and unchanged UNKNOWN recovery. An early-failure variant preserves the original exception through the same complete disposal. The existing five-second finalizer budget is unchanged. [Cleanup-control tests](../../tests/conformance/test_lifetime_cleanup_controls.py) retain `test_failure_disposal_closes_unadopted_bootstrap_descriptors`; [storage-bridge tests](../../tests/conformance/test_conformance_bridge_storage.py) retain the owning integration.

## Browser installation repair

The same run's browser job was cancelled during dependency setup; it supplied no browser execution evidence. The repair routes the existing Ubuntu mirror-list source to the Ubuntu HTTPS archive while retaining package-signature authentication, signing keys, distribution/security sources and the installed dependency set. Read-only GitHub permissions, pinned tools/actions, frozen frontend install, full `playwright install --with-deps chromium`, required browser suite/artifact and ten-minute job deadline remain.

[Browser CI checks](../../tests/architecture/test_browser_ci.py) retain `test_browser_setup_preserves_authenticated_installation_and_hard_checks`. [Current development guidance](../../docs/development.md#browser-checks) and the [current workflow](../../.github/workflows/ci.yml) own actual browser commands and acceptance; the original mirror observation establishes no throughput guarantee.

## Evidence limits

Both owning regressions failed before their corresponding corrections and passed afterward, with separate original-failure-preservation coverage. Local passes and source review did not qualify the cancelled hosted job or later input revisions. The descriptor failure, cancelled browser setup and original whole-workflow timing failure remain their original outcomes. This repair did not change recovery authority, product deadlines, artifact/schema/vector bytes, aggregate-limit decisions or production runtime behavior. Current numerical classification and merge acceptance follow the [approved audit](approved-verification-audit-2026-10-29.md) and [bounded MVP amendment](bounded-mvp-verification-2026-10-29.md); no existing hard deadline or resource/browser check is removed by this historical condensation.
