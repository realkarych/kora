# Retirement of unused schema implementations

Implementation record for [#1598](https://github.com/realkarych/kora/issues/1598), checked on 2026-11-15 against `0edca30961c19aff443c01e970626ef66d22c5c1`. The owner approved the [bounded sequencing exception](https://github.com/realkarych/kora/issues/1598#issuecomment-6895307201), recorded in [standard-schema verification and delivery](../specs/schema-validation.md#7-verification-and-delivery). [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and that specification govern the replacement; this record maps the retired implementation to its surviving obligations.

## Removed and preserved boundaries

The removed `kora.ir.structured_schema_adapter` was an incomplete, single-schema v1 adapter. The removed `kora.engine.parameter_schema_support` checked callable positions in an older parameter arena. They had no production caller, re-export or registered entry point. Their complete handwritten production diff is 673 plus 276 deletions, with no production additions. The replacement source check and native decision already exist under #1585 and #1582. Retirement does not activate a consumer or make a historical binding executable.

The caller inventory covered production, tests, configuration and documentation. Its executable references were confined to the two dedicated test modules and the source/selection tests described below. Historical CI duration rows for deleted test modules remain measurements: the planner assigns actual collected nodes, not every historical table entry. Historical research records describe their original revisions rather than the current implementation.

`graph_execution_contract` still uses `parameter_schema_source` and `parameter_schema_references`; neither changes. `structured_schema_evaluation_policy` remains the saved v1 reader used by `graph_outputs` and `graph_schema_preparation`. Live source/preparation/reference paths, the other legacy evaluator groups, their tests and exact budget rules remain. No schema format, immutable digest, supported domain, limit, dependency, numerical method, diagnostic event or resource owner changes.

## Retained behavioral coverage

The new cases live in [test_standard_schema_retained_cases.py](../../tests/ir/test_standard_schema_retained_cases.py). Both exact v2 domains exercise shared behavior; large-integer output cases deliberately use only the output domain. They call the real source checker and validator, with no replacement library behavior. The unchanged [semantics](../../tests/ir/test_standard_schema_semantics.py), [source](../../tests/ir/test_standard_schema_source.py), [admission](../../tests/ir/test_standard_schema_admission.py), [source bounds](../../tests/ir/), [source lifetime](../../tests/ir/test_standard_schema_source_lifetime.py) and [value lifetime](../../tests/ir/test_standard_schema_lifetime.py) suites retain the remaining accepted obligations.

| Retired behavior | Surviving named checks |
|---|---|
| Primitive numeric/type/length decisions, including canonical decimal versus binary float and Unicode code points | `test_standard_schema_numeric_view_preserves_decimal_semantics`, `test_standard_schema_supported_keyword_families`, `test_standard_schema_retains_exact_primitive_boundaries` |
| Assertions inapplicable to a native kind; unneeded membership/unique index work | `test_standard_schema_retains_inapplicable_assertions`; the replacement owns no old frame membership index |
| Required/dependentRequired membership, child and sibling requirements, recursive equality and uniqueness | `test_standard_schema_retains_membership_without_truthiness`, `test_standard_schema_numeric_view_preserves_decimal_semantics`, `test_standard_schema_supported_keyword_families` |
| Standalone contains bounds and independent then/else/annotation no-ops | `test_standard_schema_retains_standalone_contains_bounds`, `test_standard_schema_supported_keyword_families` |
| Original closed references, both reference occurrences and their sibling type | `test_standard_schema_adapter_survives_dialect_and_reference`, `test_standard_schema_both_references_are_independent_assertions`, `test_standard_schema_retains_sibling_type_after_both_references` |
| Original named root retained through containment and local references; equal roots remain distinct | `test_standard_schema_retains_named_root_through_containment`, `test_standard_schema_literal_markers_and_equal_roots_remain_distinct`, `test_standard_schema_source_equal_documents_keep_distinct_roots` |
| Closed registry authority and literal marker isolation | `test_standard_schema_source_refuses_unresolved_or_non_schema_targets`, `test_standard_schema_source_root_id_does_not_rebase_local_references`, `test_standard_schema_literal_markers_and_equal_roots_remain_distinct`, `test_standard_schema_original_pointer_never_enters_shared_parser_cache` |
| Refusal distinct from completed false; exact input/domain admission | `test_standard_schema_hidden_support_refusal_is_never_schema_false`, `test_standard_schema_source_bindings_are_exact`, `test_standard_schema_native_domain_refuses_before_truth`, `test_standard_schema_supported_keyword_families` |
| Schema call depth separate from JSON/payload depth; completed suffix revisited on a longer route | `test_standard_schema_retains_shared_suffix_depth`, `test_standard_schema_preflight_bounds_reference_depth`, `test_standard_schema_source_depth_uses_original_document_origin`, `test_standard_schema_capacity_boundaries` |
| Local and named-document cycles, including both reference forms | `test_standard_schema_retains_cross_document_cycle_refusal`, `test_standard_schema_source_graph_support` |
| Support of callable groups, exclusions, nested resources and complete keyword arguments | `test_standard_schema_source_arguments`, `test_standard_schema_source_checks_every_document`, `test_standard_schema_source_requires_actual_constraint_roots`, `test_standard_schema_source_refuses_malformed_or_ambiguous_documents` |
| Exact source/value capacities, independent bounds, repeated reference/containment edges and aggregate admission | `test_standard_schema_source_capacity_boundaries`, `test_standard_schema_source_position_boundary_includes_boolean_children`, `test_standard_schema_call_weights_and_root_multiplicity`, `test_standard_schema_preflight_bounds_expansion`, `test_standard_schema_combined_admission_refuses_before_construction` |
| Completion, failed membership, iterator/error/traceback and temporary traversal release | `test_standard_schema_errors_release_owned_values`, `test_standard_schema_refusal_drops_original_exception_graph`, `test_standard_schema_source_releases_owned_state`, `test_standard_schema_source_refusal_drops_exception_frames_without_collection`, `test_standard_schema_resolver_histories_are_bounded_and_released` |
| Content-free completion and failure-independent diagnostics | `test_standard_schema_diagnostics_exclude_content`, `test_standard_schema_source_diagnostics_exclude_content`, `test_standard_schema_survives_unavailable_sink`, `test_standard_schema_source_survives_unavailable_sink` |

The retained source test `test_schema_source_preserves_canonical_scalars_in_ordered_containers` no longer calls the removed checker. Its exact key order, wide integer, signed zero, canonical spelling and lone-surrogate assertions remain. It tests the historical source domain; replacing its checker call with a v2 decision would incorrectly impose v2's stricter input domain.

`test_selection_closes_typed_inert_references_without_executing_them` likewise removes only the obsolete support invocation/result. It still verifies the exact selected named closure, distinct owners for equal documents, original document root and actual resolved Boolean value. Selection is not schema-support authority, so its inert-reference closure test remains meaningful independently of the retired traversal.

## Superseded implementation-specific rules

The deleted dedicated tests also checked details that the accepted replacement intentionally does not preserve:

| Retired checks | Accepted replacement |
|---|---|
| Complete decision ledgers, atomic unpaid-action fences, initial handler ticket, primitive shared-meter charges and no meter refund/reset | V2 admits the complete deterministic certificate before the numerical view/library operation. Source/admission suites preserve exact capacity and refusal boundaries; old per-action totals are not v2 limits. |
| Frame-local membership index reuse, child index ownership and prepaid index stores | The v2 certificate accounts for its actual view/library owners. Value lifetime and allocation tests cover those owners; the old `_Frame` index no longer exists. |
| Every unfinished dispatch slot, fixed availability errors and their allocation ticket | V2 supports the accepted complete keyword set using the pinned library and three owned handlers. An incomplete handler guard is not part of v2. |
| Canonical early stopping before an unavailable handler, dynamic-reference visitation order and old support-error precedence | V2 validates every supplied schema position before evaluating truth. Both reference assertions and their sibling assertions survive; old short-circuit refusal ordering does not. |
| Callable-only support ignoring inert definitions or a standalone `if` until referenced | V2 checks all schema positions, including otherwise unselected branches and cycles. Source all-document/argument/graph tests and hidden-support refusal protect the replacement rule. |
| Depth 64, traversal frame/entry budgets, outgoing-position charges, both-reference charges and invalid private limit arguments | V2 exact bindings use root-inclusive call depth 32 and their own source/certificate limits. Depth, repeated-edge, capacity and exact-binding tests remain blocking. Historical source/reference budgets stay separately tested. |
| Inexact private prepared handles and the old driver rejecting every library error yield | V2 admits its native input/binding boundary and consumes bounded internal validation errors with explicit iterator closure. Native refusal, error-release and resolver ownership tests cover the new boundary. |
| No event from the incomplete v1 operation | V2 has its already implemented caller-bound, content-free DEBUG completion event. Diagnostic safety and sink-failure behavior remain tested. |

The historical [adapter implementation record](structured-schema-adapter-foundation-2026-11-01.md) and v1 specification retain their original meaning; they do not claim that removed source remains available on the current branch. Saved v1 contracts still do not gain new execution eligibility.

## Verification limits

The latest available master mutation report inspected during planning was [43705847856](https://github.com/realkarych/kora/actions/runs/43705847856), source/tooling `2dff3824df3b93d2a0937300219a3978c770cec5`. Its executed findings covered pricing and CEL paths, not these schema components. It supplies no applicable finding or schema mutation coverage and creates no additional PR campaign.

Focused source/selection/reference, replacement-schema, import-layer and no-suppression tests, strict static checks, independent correctness review of the complete diff and final-head CI remain required. Actual commands, revisions, results and CI links belong to the pull request. This record does not substitute for executable evidence. No timing or process-memory guarantee is introduced.

Complete saved producer/reader bindings, parameter composition, real consumer reservations/cancellation and storage/Worker/scheduler activation retain their separate implementing issues and gates. The sequencing exception applies only to the two unused implementations removed here.
