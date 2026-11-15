# Retirement of the unused parameter evaluator

Issue [#1619](https://github.com/realkarych/kora/issues/1619) removes the unused engine evaluator after complete output composition/publication (#1594/#1595) and parameter composition/publication (#1612). The baseline is `83972cf14ce648aadd6339ace7eff8756d254d73`, the #1618 squash. This follows [standard-schema delivery order](../specs/schema-validation.md#7-verification-and-delivery) and [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md); it introduces no sequencing exception or new interpretation.

## Caller and ownership inventory

The removed production group is exactly `engine/parameter_schema_evaluation.py` (501 lines), `parameter_arena_predicates.py` (125) and `parameter_numeric_predicates.py` (109): 735 deletions, zero additions. The complete baseline import/name search over `src`, `tests`, `pyproject.toml`, `.github` and `scripts` found only the evaluator's imports of its two predicates, the six dedicated test modules below, and historical test-duration rows. A second search for their exported functions, enums and exceptions found no external production caller or registration. None was an executable saved-policy binding. The current parameter composition imports the standard validator and the separate live source/reference facilities.

The change preserves `parameter_schema_source`, `parameter_schema_scalars`, `parameter_schema_selection`, `parameter_schema_references`, their four test modules, and every graph writer/reader. It also preserves `structured_schema_evaluation_policy` and the old IR assertions, equality, collections, cursors, numbers and work meter with their own tests. Those remaining groups have distinct later retirement ownership. Historical research and `scripts/ci/lane_durations.json` keep their original measurements; the lane planner selects collected tests, not missing modules from historical duration rows.

The old engine tests directly exercised `arena_object_lookup` as well as the retired wrappers. Its exact `[3, -1, -1]` ordinal result moves to `tests/ir/test_structured_schema_cursors.py::test_arena_lookup_returns_sorted_member_ordinals`, using a real frozen arena without the retired engine helper. Wrapper-specific cumulative totals do not become the cursor's public contract. The surviving IR tests still own the unchanged primitive schedules, refusal and release assertions.

## Replacement cases and scope

All new semantic cases run `validate_standard_schema` with the exact PARAMETERS binding. They do not infer parameter coverage from OUTPUT-only tests. `tests/ir/test_standard_parameter_retained_cases.py` supplies these named checks:

- `test_standard_parameters_retain_scalar_shape_and_presence`: signed-domain numeric boundaries, inclusive/exclusive comparisons, nonnumeric type alternatives, Unicode code-point lengths, immediate container counts, large schema counts, NUL/astral property names and no Unicode normalization.
- `test_standard_parameters_retain_recursive_equality`: sorted object keys, array order, canonical-decimal versus binary-rounded integers, Unicode inequality, enum and uniqueness, and depth18 equality within the parameter domain. Original value/source serialization stays unchanged.
- `test_standard_parameters_retain_logical_and_array_decisions`: Boolean roots, logical and conditional branches, annotation-only legacy keywords, prefix/tail/nested arrays and exact contains bounds.
- `test_standard_parameters_refuse_invalid_source_before_applicability`: hidden cyclic references and malformed keyword arguments refuse before value applicability. A branch or non-array value cannot hide an unsupported source.

Existing tests supply the rest of the replacement obligations:

| Surviving check | Preserved obligation |
|---|---|
| `test_standard_schema_numeric_view_preserves_decimal_semantics` (both domains) | Decimal multipleOf, signed zero, integral float classification, bool/number distinction at every nesting level, uniqueItems and immutable source/value serialization |
| `test_standard_schema_retains_inapplicable_assertions` (both domains) | Well-formed inapplicable numeric, size, membership and uniqueness constraints |
| `test_standard_schema_retains_membership_without_truthiness` (both domains) | Null/false presence, absent triggers and required siblings through references |
| `test_standard_schema_retains_standalone_contains_bounds` (both domains) | minContains/maxContains without contains are annotations |
| `test_standard_schema_native_domain_refuses_before_truth` (both domains) | Native-only types, nonfinite values, surrogate text and no authored callbacks |
| `test_standard_schema_parameter_integer_domain_is_signed` | Signed-64 parameter endpoints and refusal outside them |
| `test_standard_schema_source_arguments`, `test_standard_schema_source_graph_support`, `test_standard_schema_source_integer_capacity_precedes_decimal_formatting` | Complete argument/support admission, hidden cycles and the v2 schema integer ceiling |
| `test_standard_schema_capacity_boundaries`, `test_standard_schema_combined_admission_refuses_before_construction`, `test_standard_schema_hidden_support_refusal_is_never_schema_false` | Replacement certificate capacities and refusal outside Boolean truth, rather than the old per-action meter |
| `test_standard_schema_errors_release_owned_values`, `test_standard_schema_refusal_drops_original_exception_graph`, `test_standard_schema_diagnostics_exclude_content`, `test_standard_schema_survives_unavailable_sink` | Shared evaluator operation disposal and content exclusion; these shared-mechanism tests do not alone prove parameter caller settlement |
| `test_parameter_composition_preserves_original_value`, `test_parameter_composition_diagnostics_exclude_original_content`, `test_parameter_holder_is_independent_of_mutable_source` | Actual parameter composition, detached immutable ownership and parameter diagnostics |
| `test_parameter_profile_round_trip_and_handoff`, `test_saved_parameter_reader_rejects_inconsistent_whole_binding`, `test_legacy_parameter_history_cannot_gain_a_current_holder` | Complete SQLite/PostgreSQL writer/checking reader and original history without new authority |
| `test_schema_source_preserves_canonical_scalars_in_ordered_containers`, `test_parameter_schema_scalar_preserves_checked_native_identity`, `test_parameter_schema_scalar_spells_integers_beyond_the_native_digit_limit` | Live source spelling and kind, signed zero and historical schema integers wider than the new executable ceiling |

The old helper admitted a 14,001-bit schema integer, while v2 explicitly caps executable schema integers at 14,000 bits. That helper's execution assertion is superseded by section 2 of the accepted specification. Saved source preservation is not removed: the surviving scalar test round-trips a 4,601-digit integer, and source tests preserve wide integers, integral floats and negative zero. Value-domain signed-64 admission remains separately enforced.

## Complete disposition of the six removed test modules

Each row covers all parameterized variants of that function. Mixed rows distinguish retained truth from obsolete internal accounting or branch-sensitive refusal. The named replacement tests above run alongside the unchanged source/history checks. The old tests remain available at the baseline revision.

### `tests/engine/test_parameter_schema_evaluation.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_schema_evaluates_direct_and_logical_assertions` | Logical/array, scalar/presence and existing decimal/membership cases preserve supported truth. The two conditional branches hiding `{$ref: "#"}` now refuse whole-source cycles, covered by invalid-source admission; section 3 forbids branch-dependent support. |
| `test_parameter_schema_skips_decided_and_orphan_children` | Logical/array cases preserve Boolean decisions and orphan `if`; custom frame allowances and early-return accounting are replaced by sections 4–5. |
| `test_parameter_schema_does_not_use_later_results_after_entered_refusal` | Old frame2 exhaustion is superseded by root-inclusive call certificates. Existing hidden-support and combined-admission tests preserve refusal outside truth for all/any/one/not/conditional contexts. |
| `test_parameter_schema_checks_type_before_unavailable_or_costly_heads` | The recursive source refuses before type mismatch under section 3, covered by invalid-source admission. The costly const case's private work threshold is replaced by the complete certificate. |
| `test_parameter_schema_refuses_entered_unavailable_heads` | Partial-implementation unavailable slots disappear. Cycles, patterns and unevaluated keywords still refuse complete support; supported properties, dependentSchemas, additionalProperties and propertyNames use the complete standard validator. Existing source and supported-keyword suites cover that accepted distinction. |
| `test_parameter_schema_and_equality_have_independent_root_inclusive_frames` | Private frame2/pair2 knobs disappear. Parameter depth18 equality remains covered; exact call-depth and capacity boundaries belong to standard admission tests. |
| `test_parameter_boolean_schema_has_exact_work_and_entry_admission` | Boolean truth moves to logical/array cases. Work34/entries5 and caller frame knobs are reserved-v1 schedule details explicitly replaced by sections 2, 4 and 5. |
| `test_parameter_schema_shares_exact_work_across_entered_direct_branches` | Work9203/bytes51/entries31 is the removed wrapper schedule. The complete multi-constraint certificate remains tested by standard admission and scalar/logical semantics. |
| `test_parameter_schema_releases_continuations_after_nested_result` | Removed generator continuations no longer exist. Standard error-release tests and actual parameter composition own the replacement; no forced-GC assertion is transferred. |
| `test_parameter_schema_refusal_releases_pending_frames_and_private_context` | Removed wrapper frames and its safe exception no longer exist. Common original-exception disposal, diagnostics and caller completion tests preserve actual replacement ownership. |

### `tests/engine/test_parameter_array_evaluation.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_items_use_exact_elements_and_skip_unused_operands` | Prefix/tail/nested-array truth moves to logical/array cases. Four hidden cyclic-reference variants and malformed prefixItems no longer bypass whole-source checking; invalid-source admission covers this section 3 change. |
| `test_parameter_contains_evaluates_exact_bounds_and_initial_interval` | Real const/type/Boolean contains bounds move to logical/array cases; standalone bounds remain in the both-domain suite. Six cases containing the intentionally unavailable recursive head, plus malformed bounds on a non-array, now refuse whole-source support. |
| `test_parameter_contains_settles_after_each_completed_element` | All three schemas hide a cyclic reference in the old unvisited tail. Section 3 replaces this early-settlement shortcut with whole-source refusal, independently of the remaining possible match interval. |
| `test_parameter_contains_detects_wide_reversed_bounds_before_interval_work` | Both old sources include a recursive unavailable child and now refuse support. The distinct mathematical wide reversed-bound result is retained using a supported child in logical/array cases; work5377 is not a v2 limit. |
| `test_parameter_array_children_share_exact_work_and_entry_admission` | Old work1788/3122 and entry18/17 schedules retire. Actual item/contains truth and standard complete-operation capacity admission remain. |
| `test_parameter_array_evaluation_propagates_entered_refusal_in_order` | All four sources contain recursive unavailable heads. Invalid-source admission preserves refusal before library execution; canonical entered-head order is not a v2 contract. |
| `test_parameter_array_evaluation_refuses_entered_malformed_bounds` | All four malformed schemas are explicit invalid-source cases; the replacement disposition is fixed `unsupported_schema`, before applicability. |
| `test_array_frame_refusal_releases_private_state` | Private array generator/frame cleanup retires with its implementation. Standard error-release, original-exception disposal and caller completion preserve replacement cleanup. |

### `tests/engine/test_parameter_numeric_predicates.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_multiple_of_uses_exact_canonical_decimals` | Existing both-domain decimal cases plus scalar/presence endpoint cases cover all five decisions. Actual parameter composition and live scalar tests preserve original numeric kind and negative zero. |
| `test_parameter_numeric_predicates_follow_json_kinds_and_bounds` | Existing decimal tests and new scalar/presence cases preserve valid numeric decisions. The two inapplicable malformed-bound shortcuts now refuse complete source arguments. |
| `test_parameter_numeric_predicate_accepts_pinned_integer_above_artifact_cap` | The 14,001-bit execution exception is superseded by the accepted v2 source ceiling; unchanged live scalar/history checks preserve wider saved data as described above. |
| `test_parameter_numeric_predicate_bounds_intermediate_scaling_separately` | The private 14/15-bit knob retires. `test_standard_schema_numeric_intermediates_are_reserved_before_fraction` owns the accepted v2 intermediate calculation; the unchanged IR arithmetic tests still own their schedule. |
| `test_parameter_numeric_predicates_share_exact_cumulative_allowance` | Work82/bytes4/entries4 for two wrapper calls retires; standard complete-operation admission replaces it. Unchanged IR work-meter tests still enforce monotone primitive counters. |
| `test_parameter_numeric_predicate_refuses_each_short_cumulative_allowance` | The three one-short wrapper dimensions retire with that wrapper. Standard capacity tests retain independent certificate admission. |
| `test_parameter_numeric_predicate_requires_explicit_nonartifact_meter` | The private wrapper meter-type requirement has no replacement API. Exact domain binding is tested by `test_parameter_binding_cannot_inherit_another_interpretation`. |
| `test_parameter_numeric_predicate_refuses_invalid_entered_divisor` | Nonpositive/Boolean divisor cases move to invalid-source admission; source argument/native-domain tests cover nonfinite and string operands. |
| `test_parameter_numeric_equality_refuses_malformed_mixed_kind_operands` | Both-domain native-domain admission and source rejection preserve no-callback/nonfinite refusal before evaluation. The private numeric-only EQUAL entry point retires. |
| `test_parameter_numeric_refusal_clears_operands_and_active_exception` | Wrapper-local traceback shape/work0 is obsolete. Common original-exception disposal and content-exclusion checks own the replacement failure boundary. |

### `tests/engine/test_parameter_equality_predicates.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_const_checks_ordered_structural_equality` | All six semantic cases remain in recursive-equality and existing both-domain decimal/bool cases. |
| `test_parameter_const_compares_all_object_keys_before_values` | Different-key inequality remains in recursive-equality cases; work400 early-decision behavior is replaced by conservative full equality admission. The surviving IR test `test_recursive_equality_compares_every_object_key_before_any_value` retains its primitive schedule. |
| `test_parameter_equality_stops_after_first_decisive_pair` | Enum match and duplicate uniqueness remain in recursive-equality cases. The old work400 cutoff is not a v2 promise. |
| `test_parameter_equality_cannot_skip_an_incomplete_entered_pair` | Branch-sensitive work400 exhaustion is replaced by preflight admission of the complete possible equality workload. Existing combined-admission tests preserve refusal before truth. |
| `test_parameter_collection_equality_respects_predicate_applicability` | Nested numeric enum, Boolean distinction, signed zero and false uniqueness remain in both-domain decimal tests and recursive-equality cases. Malformed uniqueItems on an object now refuses complete source admission. |
| `test_parameter_equality_uses_caller_pair_depth_above_artifact_default` | Parameter depth18 equality is an explicit recursive-equality case. Caller pair18/19 controls retire; the accepted parameter/source/call depth checks remain distinct. |
| `test_parameter_equality_admits_exact_float_pair_work_bytes_and_entries` | Work8390/bytes100/entries9 is the retired wrapper schedule, superseded by the complete certificate. Shared decimal and IR arithmetic checks remain. |
| `test_parameter_equality_shares_allowance_across_completed_and_refused_calls` | Private work52/103 accumulation retires; unchanged IR meter tests still own their cumulative primitive guarantees. |
| `test_parameter_equality_refuses_independently_short_float_pair_allowance` | All three private one-short cases retire with the wrapper; standard capacity/combined-admission tests cover replacement bounds. |
| `test_parameter_equality_refuses_negative_cursor_index` | The removed wrapper no longer accepts cursors. Native source/value admission replaces its boundary; unchanged IR cursor facilities retain their own tests. |
| `test_parameter_equality_clears_active_pair_and_private_exception_context` | Removed pair-wrapper frames retire. Standard actual-release/original-exception checks and parameter composition retain replacement ownership and content exclusion. |

### `tests/engine/test_parameter_property_predicates.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_property_requirements_use_exact_applicable_presence` | Unicode/NUL and no-normalization move to scalar/presence cases; both-domain membership retains absent/null/false behavior. Four malformed inapplicable requirements now refuse whole-source admission. |
| `test_parameter_required_preserves_saved_name_order_under_budget` | Private work300 shortcut retires. V2 preflights all possible membership work; unchanged IR required-name tests still preserve their saved-order primitive contract. |
| `test_parameter_dependent_requirements_use_canonical_trigger_order` | Private work300 branch behavior retires; the existing IR dependent-required tests retain their primitive schedule, and both-domain membership tests retain truth. |
| `test_parameter_property_lookup_prepays_exact_late_row_comparisons` | Wrapper work162/161/158, including its surrounding charges, retires. The direct lookup ordinal assertion is moved below; surviving IR membership/cursor tests retain their schedules. |
| `test_parameter_property_lookup_returns_sorted_member_ordinals` | Moved directly to `test_arena_lookup_returns_sorted_member_ordinals`; the exact `[3, -1, -1]` result survives independently of the removed helper. |
| `test_parameter_entered_malformed_property_requirement_clears_context` | Both malformed sources are explicit invalid-source cases. The common refusal and diagnostic boundary replaces the private wrapper traceback. |

### `tests/engine/test_parameter_shape_predicates.py`

| Removed test | Disposition |
|---|---|
| `test_parameter_type_uses_exact_json_kinds` | All eleven decisions remain in scalar/shape and both-domain decimal type cases. |
| `test_parameter_type_alternatives_follow_saved_order_on_capacity_refusal` | Work300 depending on number/integer order is replaced by complete preflight. The unchanged IR leaf-type tests retain their own old schedule. |
| `test_parameter_size_assertions_compare_exact_inclusive_counts` | All thirteen cases are explicit scalar/shape cases, including mathematical huge bounds. |
| `test_parameter_size_assertions_do_not_enter_inapplicable_operands` | All six used malformed string bounds. Section 3 requires complete source argument admission; invalid-source cases plus both-domain well-formed inapplicability preserve the accepted distinction. |
| `test_parameter_item_count_uses_exact_work_without_visiting_children` | Private work83/82 retires. Immediate element counts remain in scalar/shape cases, while all value content is admitted before the v2 decision. |
| `test_parameter_shape_refuses_raw_string_dispatch_token` | No token-dispatch wrapper remains. Exact policy/profile binding has independent tests and no coercion. |
| `test_parameter_shape_refusal_clears_private_operands_and_ambient_context` | Malformed source admission and common content-free disposal replace the removed wrapper traceback. |

## Verification and remaining limits

Focused verification runs the new parameter cases and cursor assertion, existing standard source/decision/admission/lifetime suites, all four live parameter-source suites, real parameter composition, whole saved parameter/output history on SQLite and PostgreSQL, and architecture/static checks. Existing deployment completion tests retain cancellation, unexpected failure, abandonment and shutdown ownership. Independent correctness checks the complete inventory and dispositions; deleting unreachable internals changes no live authority, resource algorithm or numerical acceptance method and selects no additional specialist profile.

The latest inspected daily mutation audit was [43705847856](https://github.com/realkarych/kora/actions/runs/43705847856), source/tooling `2dff3824df3b93d2a0937300219a3978c770cec5`, attempt1: partial due to budget, 188 executed, zero reused, pricing/CEL scopes only. It contains no applicable schema result and adds no campaign or gate. Actual commands, revisions, independent results and final-head/master CI evidence belong to the PR.

This deletion grants no accepting parameter handoff, accepted output, Worker report or scheduler settlement. Remaining source facilities, historical immutable readers and later IR retirement groups retain their owners. No migration, stored-policy rewrite, provider ABI change, dependency change, timeout increase or CI-gate change occurs.
