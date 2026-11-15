# Schema cursor and numerical primitive retirement

Date: 2026-11-15. Implementing issue: #1628, final enumerated retirement group of #1361 after #1625. Source inventory baseline: `4de65b8e0b12cd26fc01d7d41dadc7ee8ceba8c9`. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and the [standard-schema contract](../specs/schema-validation.md) govern the accepted replacement. This record maps removed implementation and tests; it does not qualify an accepting consumer.

## Removal and retained owners

The unused `structured_schema_cursors.py`, `structured_schema_numbers.py` and `structured_schema_evaluation_work.py` contain 329, 167 and 185 production lines respectively: 681 deletions, with no replacement production code. Their only production imports are within this group. Their three dedicated test modules contain 40 test functions. Module/export-name inventory covers source, tests, entry points, configuration and scripts; no outside production caller exists at the recorded baseline. Historical lane durations describe past measurements and do not register tests for execution.

The still-live `structured_schema_evaluation_policy.py` remains the exact historical output policy and refusal vocabulary used by graph output readers and preparation. `structured_schema_work.py`, input/snapshot/preparation, original scalar spelling and closed references remain. Parameter source/scalar/selection/reference facilities, model identity, complete output/parameter composition and saved graph readers are unchanged. Removing the private v1 evaluator primitives grants neither v2 defaults to historical records nor accepted-output, Worker or scheduler authority.

V2 replaces the private v1 per-action work/byte/entry and 16,384-bit intermediate schedules with its accepted preflight certificate and 32,768-bit intermediate bound. Old work exhaustion, borrowed cursor ordinals, index layouts and DecimalNumber representation are not promises of the current evaluator. The normative historical schedule and its evidence remain; this retirement does not silently weaken a current gate.

## Live tests preserved exactly

The three `test_actual_*` functions, their decorators and hostile input classes move unchanged from `test_structured_schema_cursors.py` to `test_structured_schema_native_admission.py`; only module imports change. They still call the real StructuredOutput model, SchemaArena and raw parser. The matrix keeps all 12 cases: string/key/integer/float/array/object subclasses, NaN, both infinities, a 14,001-bit integer and surrogate string/key. Cycles still refuse at both model and arena. Raw NaN, duplicate decoded names and both surrogate positions retain SO002; native model and arena retain SO003 and unsupported_schema separately.

## Numerical and identity preservation

`test_standard_schema_numeric_retained_cases.py` uses the exact OUTPUT binding, which admits the historical wide artifact integers independently of the narrower PARAMETERS domain. It executes the four frozen scalar vectors from `StructuredOutput.vectors.json`, rather than substituting handwritten expected values. Its twelve original ordering pairs assert equality and strict ordering in both directions and unchanged input serialization. The existing eight-case divisibility matrix remains, with maximum finite float divided by one added separately. Four missing integer-classification cases supplement the six already present in the scalar cross-product.

The current numerical equality operation is explicitly joined to canonical bytes and distinct digests for integer/float one and signed floating zero. The model's full canonical-vector suite remains separate. The new authored-metaclass refusal test uses both current value and schema admission, in both domains; it asserts no equality or representation callback, safe output and immediate input release without forced collection. It does not mistake an ordinary hostile instance for a hostile metaclass. Empty and long shared-prefix text equality supplements existing NUL, supplementary-code-point and normalization distinctions.

## Complete old-test disposition

Every row includes all original parameterized variants. Private schedule/representation rows retire with their complete implementation, not with a replacement assertion that the old schedule passes under v2. Mixed rows identify the surviving semantic or actual owner explicitly.

### Cursor suite: 12 functions

| Original function | Disposition |
|---|---|
| `test_arena_lookup_returns_sorted_member_ordinals` | Retire private sorted-member ordinal `[3, -1, -1]`; original schema/root/reference selection remains in standard source and retained reference suites. No current API exposes that ordinal. |
| `test_native_index_collects_borrowed_rows_with_exact_bulk_and_header_charges` | Retire empty/singleton/reverse/Unicode-prefix/long-prefix private sorted-index representation, borrowed-row identity and collection/header charges. Current object equality and membership retain Unicode/key and source-identity semantics in `test_standard_schema_retains_recursive_literal_equality`, `test_standard_schema_retains_membership_without_truthiness` and `test_standard_schema_numeric_view_preserves_decimal_semantics`; no new sorted-index interface remains. |
| `test_native_index_lookup_uses_prepaid_binary_search_and_no_new_entries` | Retire six private query/ordinal and prepaid-search cases; current presence/absence decisions remain in the membership suite. |
| `test_native_index_empty_lookup_uses_only_empty_search_charge` | Retire empty private-index charge; empty required/dependency forms retain real standard decisions. |
| `test_native_index_reserves_fixed_headers_and_variable_cells_before_allocation` | Retire work/entries × exact/one-short private collection schedule and index repr; current aggregate admission remains in `test_standard_schema_capacity_boundaries`. |
| `test_native_index_failed_reservations_keep_only_the_paid_prefix` | Retire all five private paid-prefix counter cases. |
| `test_native_index_lookup_precharges_its_complete_binary_search` | Retire exact/one-short private search charge. |
| `test_native_index_lookup_charges_longest_stored_key` | Retire present/missing × exact/one-short maximum-key bookkeeping; current membership cases retain long-key presence/absence without claiming a private index. |
| `test_native_string_comparison_reserves_one_codepoint_comparison` | Retire exact/one-short charge; preserve empty and 2048-character common-prefix decisions in `test_standard_schema_retains_empty_and_long_prefix_equality`, and NUL/supplementary/no-normalization equality in the retained recursive-literal suite. |
| `test_actual_model_and_arena_admission_refuse_hostile_domain_without_callbacks` | Exact relocation, same name, all twelve cases and both real owners. |
| `test_actual_model_and_arena_admission_refuse_cycles` | Exact relocation, same name, both refusal assertions. |
| `test_actual_raw_admission_refuses_duplicate_names_and_surrogate_text` | Exact relocation, same name, all four raw inputs and SO002. |

### Numerical suite: 14 functions

| Original function | Disposition |
|---|---|
| `test_structured_schema_numbers_execute_published_scalar_vectors` | `test_standard_schema_executes_published_scalar_vectors` executes all four original named vectors with the actual current OUTPUT decision. |
| `test_structured_schema_exact_decimal_divisibility` | Existing `test_standard_schema_retains_divisibility_extremes` preserves eight original pairs; `test_standard_schema_maximum_float_is_an_integer_multiple` preserves the ninth. |
| `test_structured_schema_exact_decimal_ordering_and_equality` | `test_standard_schema_retains_numerical_order_and_equality` preserves all twelve exact original pairs, equality and both order directions: decimal neighbors, both zero forms, positive/negative fractions, zero/subnormal, ordered subnormals, largest finite decimal, largest admitted integers and negative wide/subnormal. |
| `test_structured_schema_integer_classifier_uses_canonical_decimal` | Six original values remain in `test_standard_schema_retains_scalar_type_cross_product`; four negative-float/maximum-float/maximum-integer cases are explicit in `test_standard_schema_retains_numeric_integer_classification`. |
| `test_structured_schema_supported_numbers_can_exhaust_alignment_work` | Retire private v1 alignment work refusal; current preflight, intermediate and combined-capacity checks remain in the standard admission suite, with no claim that the old refusal must recur. |
| `test_structured_schema_numerical_equality_preserves_payload_identity` | `test_standard_schema_numerical_equality_preserves_payload_identity` joins current schema equality to all four exact canonical encodings and both digest inequalities. |
| `test_structured_schema_decimal_record_has_only_coefficient_and_exponent` | Retire all six DecimalNumber shape/coefficient/exponent/repr cases; the current mathematical interpretation and original identity have separate tests and no compatible record-shape obligation. |
| `test_structured_schema_numbers_refuse_non_native_or_nonfinite_scalars` | Retire the numeric-only entry point. Real native admission preserves subclasses/NaN/infinities; standard scalar type tests preserve boolean separation while keeping booleans valid JSON. |
| `test_structured_schema_numbers_check_integer_bit_capacity_before_arithmetic` | Both signs and inclusive/over-limit native/raw cases remain in `test_structured_output_integer_magnitude`; standard value/source bounds stay separate. Preparation scalar tests retain the process digit-limit check. Borrowed coefficient identity and zero private allocation retire. |
| `test_structured_schema_multiple_of_refuses_nonpositive_divisor` | All five zero/signed-zero/negative operands remain in `test_standard_schema_retained_unvisited_forms_refuse_whole_source`; safe refusal and actual exception disposal remain at current owners. |
| `test_structured_schema_numeric_boundaries_never_invoke_authored_metaclasses` | Retire the four deleted wrappers. `test_standard_schema_native_boundary_refuses_authored_metaclasses` preserves exact metaclass callback refusal, content exclusion and actual release at both current domains and source/value positions. |
| `test_structured_schema_numeric_release_after_caller_disposes_refusal` | Retire deleted-helper traceback behavior. `test_standard_schema_refusal_drops_original_exception_graph`, actual effective-composition held-traceback checks and deployment completion preserve current ownership; caller deletion alone is not settlement. |
| `test_structured_schema_numeric_records_release_after_success_and_refusal` | Retire DecimalNumber-specific forced-GC enumeration and cumulative charges. `test_standard_schema_errors_release_owned_values` checks actual live errors, engines, closed generators and input owners on valid/invalid/refused paths without forced collection. |
| `test_structured_schema_internal_parse_state_releases_after_unexpected_error` | Retire removed digit-parser temporary and exact byte count. Effective-composition lifetime and deployment-completion tests retain unexpected failure, exception-group, cancellation and actual slot-settlement coverage. |

### Evaluation-work suite: 14 functions

| Original function | Disposition |
|---|---|
| `test_structured_schema_evaluation_reservations_are_inclusive_and_atomic` | Retire work/bytes/entries private counter mutation; current value/source/certificate bounds remain. |
| `test_structured_schema_integer_primitives_follow_the_normative_schedule` | Retire all twelve private arithmetic/result operations and their exact charges. |
| `test_structured_schema_integer_primitives_precharge_exact_owned_capacity` | Retire nine operations × work/bytes × exact/one-short private schedule; current finite admission and release remain. |
| `test_structured_schema_intermediate_bit_reservations_are_conservative` | Retire four operations × inclusive/one-over private 16,384-bit schedule; the current explicit 32,768-bit bound remains independently tested. |
| `test_structured_schema_zero_multiplication_reserves_no_magnitude` | Retire left/right zero shortcut charges. |
| `test_structured_schema_zero_negation_reserves_no_magnitude` | Retire private zero-negation charge. |
| `test_structured_schema_nonzero_scaling_reserves_guard_and_cursor_work` | Retire zero-step/two-step × exact/one-short scaling schedule. |
| `test_structured_schema_scaling_retains_predicates_before_failed_bit_guard` | Retire private prefix charge. |
| `test_structured_schema_failed_reservation_precedes_native_division` | Retire private failed-reservation precedence over zero division; actual whole-source nonpositive-divisor refusal remains. |
| `test_structured_schema_evaluation_counters_are_shared_and_never_refunded` | Retire private cumulative counter state; current whole-operation certificate is not reset per branch. |
| `test_structured_schema_numeric_shortcut_schedule` | Retire sign/zero/zero-step charges; cross-sign wide/subnormal decisions remain in `test_standard_schema_retains_numeric_bound_order`. |
| `test_structured_schema_float_conversion_precharges_all_owned_state` | Retire work/bytes/entries × exact/one-short conversion schedule; current numerical-view admission retains its own entries/bytes/intermediate obligations. |
| `test_structured_schema_repeated_conversions_share_counters_without_a_cache` | Retire repeated DecimalNumber identity and cumulative work; actual standard lifetime checks preserve absence of retained operation state. |
| `test_structured_schema_conversion_record_entries_are_cumulative` | Retire exact/one-short private record count. |

## Verification boundary

The PR records actual commands, tested revisions, artifacts, independent correctness and final-head CI. Required coverage includes standard source/decision/admission/lifetime/composition, the exact relocated native tests, standalone model/type-schema checks, retained parameter sources, immutable output/parameter history on real SQLite and PostgreSQL, and deployment completion. Strict Ruff/Pyright, import/no-suppression/documentation checks and ordinary link checking remain required. No timing, RSS, mutation or additional acceptance campaign is introduced. Master CI must complete after squash.

No new diagnostic event or resource owner is introduced. Existing safe content and blocked/broken-sink cases remain required. Saved historical policy identities, bytes, digests and matching replay do not gain current defaults; true schema validation alone never proves provenance, retained content, projection or physical closure.
