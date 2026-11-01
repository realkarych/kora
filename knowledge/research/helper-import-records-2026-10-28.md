# Canonical lifetime records in one-shot helper imports

Historical disposition, condensed 2026-11-01. Issues #395 and #422 are completed. The [immutable original](https://github.com/realkarych/kora/blob/c407647ee16e4969e934479f1c3a6cb197dd3e95/knowledge/research/helper-import-records-2026-10-28.md) retains source/schema hashes, canonical-definition comparisons, exact approvals and failed qualification/integration history.

## Result and surviving boundary

Canonical lifetime and identity definitions moved to model-light leaves with explicit same-object exports through existing supported import paths. Shared UTF-8 functions moved to the model-free `ir.text` leaf, with the old `ir.json` exports retained. Supported JSON schemas, validation, equality/hash identity, journal/helper bytes and operation bodies were preserved. `ProcessIdentity.__module__` changed, including the name a newly emitted pickle would use; no supported metadata/pickle consumer was found, and accepted persisted formats serialize JSON fields. This is not a pickle-compatibility guarantee.

The extraction cuts unrelated model construction from one-shot helper imports without changing launchers, observations, claims, descriptor acknowledgment, publication, cleanup or package-layer authority. The final UTF-8 repair restores eager IR diagnostic construction while avoiding the JSON import edge: a preceding lazy-export repair allowed Pydantic's first plugin scan to encounter a deliberately malformed test distribution.

[Light-import regressions](../../tests/conformance/test_conformance_plugin_import_is_light.py) retain `test_fixed_lifetime_helper_imports_no_graph_models`, the provider/logging/CEL/parent-lifetime import exclusions and canonical record identity. [Closed-model checks](../../tests/protocols/test_protocol_models_are_closed.py) retain encoding/schema boundaries; [provider-loading isolation](../../tests/protocols/test_provider_loading_imports_only_the_requested_entry_point.py) retains discovery behavior. [ADR-0026](../../docs/decisions/0026-package-layers.md) and the [package layer table](../architecture/detailed.md#package-layers) govern imports. No loader or runtime authority is added.

## Failed evidence and limits

- [CI 40739263464](https://github.com/realkarych/kora/actions/runs/40739263464) failed the macOS installed-storage command's unchanged sixty-second deadline. One local probe of the same candidate source passed but did not explain the hosted timeout or qualify CI.
- [Qualification 40818020071](https://github.com/realkarych/kora/actions/runs/40818020071) completed all 270 command cycles and reaped all parents, but the job was cancelled at mandatory completion and skipped final environment synchronization. The workflow remains non-passing; complete child populations cannot substitute for successful final job steps.
- [CI 40871178157](https://github.com/realkarych/kora/actions/runs/40871178157) failed the restored helper JSON import edge. The first lazy-export repair also failed [CI 40873679464](https://github.com/realkarych/kora/actions/runs/40873679464) at provider loading. Both failures remain failed, independently of the final model-free text repair.

Cumulative helper import totals and local probes are diagnostics, not critical-path savings, a hosted latency guarantee or complete qualification. Historical measurements retain their original producer/input identity; changed inputs require applicable fresh execution. Current merge acceptance follows the [bounded MVP amendment](bounded-mvp-verification-2026-10-29.md); exact-release local-tooling qualification remains separate under [current development guidance](../../docs/development.md), with unchanged workload, resource assertions and hard deadlines.
