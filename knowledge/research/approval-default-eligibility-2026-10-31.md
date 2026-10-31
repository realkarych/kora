# Omitted approval predicate eligibility evidence

Date: 2026-10-31. Owner: [issue #526](https://github.com/realkarych/kora/issues/526), scoped CEL–IR integration in work item 3 under accepted #137. Base: `1bfd14f0f14c3d493eecb4a963cc6ecf764e68e2`.

## Delivered boundary

`validate_approval_defaults` examines only omitted human-input predicates. Only omitted forms use the existing built-in form. Authored empty/custom forms need a declared mapped `decision` and the existing fixed expression must pass the existing Boolean checker. The result contains shared diagnostics and scalar accounting, never a checked expression, schema, environment, persisted default or response value.

Reference resolution uses saved custom schemas and exact immutable installed built-in resources, without registry/network access. Existing `$ref`, single-schema `allOf`, optional-field, enum, numeric/Boolean and `dyn` rules are reused. Boolean `false` and mixed-kind enums can map to `dyn`; this check supplies no schema-truth or response-validity decision. A `SchemaError` prevents only that dependent check, increments unresolved accounting and emits no duplicate predicate finding. Existing schema passes remain required.

This preserves TM-04/TM-10's untrusted-schema/work ownership, TM-06/TM-07's content exclusion, TM-11's saved-only interpretation and TM-08's separation from approval authority. Existing threat-model entries cover this boundary; actors, controls and runtime assumptions do not change. Contributor review and CI provide the ordinary TM-12 evidence. No `docs/` wording changes.

## Verification plan and observations

Independent correctness, security and performance design contexts inspected the proposed scope at the base revision and returned passed with implementation evidence pending. Actual-result review uses those same independent contexts and the complete implementation diff; the issue/PR records their actual provenance and conclusions. A planning verdict is not implemented-behavior evidence.

The focused semantic, exact-resource and diagnostic modules are `tests/ir/test_approval_defaults.py`, `tests/ir/test_approval_default_resources.py` and `tests/observability/test_approval_default_logging.py`. The first run found one incorrect fixture expectation: the existing branch fixture already authors one predicate. The corrected assertion preserves that one syntax invocation while omitted human-input predicates remain absent from authored syntax checking. The subsequent run passed all 86 cases on macOS Python 3.11.17. Initial strict typing found missing precise local annotations and an ambiguous test-helper forwarding argument; those test typing issues were corrected without production behavior changes. The subsequent related CEL/IR and architecture run passed 1,707 cases in 44.86 seconds. Full Ruff format/lint and strict Pyright passed after installing the repository’s existing build group, matching CI setup; no dependency or lockfile changed. Independent actual-result review and supported-platform final-head CI remain pending until recorded on the PR.

Resource assertions use deterministic existing `Meter` work and exact object release, not process-wide byte deltas. Workloads cover 1/100/1,000 omitted defaults, 1,000 references to a shared 1,000-level saved form, a 5,000-level form, known invalid and unresolved inputs, unused/shared/nested stored bodies, 3,000 errors under count/byte pressure, and real blocked/broken log pipes. The result survives while graph/config/schema/mapper/record/checked-expression owners are released; disposing of the result releases its diagnostics. Tests do not measure elapsed time, RSS or heap growth and establish no such numerical guarantee.

One call-local mapper memoizes shared schema roots. Per-node `check_typed` may still traverse reachable type, bound and snapshot metadata for every omitted predicate: the resulting work includes the sum of each expression's reachable metadata. This implementation does not claim the prepared-environment additive bound or a universal linear graph-validation guarantee. Eligibility caching is unnecessary for the currently approved scoped obligations.

## Remaining activation

General input/parameter CEL environments, explicit predicate typing, canonical rendering round-trip and native binding compilation, saved default/effective execution contracts, response validation, downstream typing/projections, compiler/API/CLI/MCP integration, authority/binding/expiry gates and approval transitions remain with their existing implementing owners. Neither a passing scoped result nor its completion diagnostic can authorize an approval or establish complete graph validation. Metadata databases are not touched by this pass; supported-platform complete CI and existing database coverage remain required on the final PR revision.
