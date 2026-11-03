# Declared graph parameters in approval predicate checks

Date: 2026-11-03. Implementation owner: [#640](https://github.com/realkarych/kora/issues/640), a child of complete approval-context work [#552](https://github.com/realkarych/kora/issues/552) supporting the execution engine [#593](https://github.com/realkarych/kora/issues/593).

## Delivered boundary

The existing partial `validate_approval_response_predicates` pass now checks explicit conditions using the effective response form and the complete declared `graph.params` schema. Root and stored-body conditions use that same graph-level schema. This implements IR 7.1 and 7.4 without a new schema view, default, execution-semantics identity or public endpoint.

The existing AST preflight records `inputs` and `params` identifiers and comprehension binders. Any input dependency defers the entire predicate, regardless of traversal order or dead branches. An actual parameter dependency with omitted `graph.params` also remains incomplete. Strings, selected field names and literal string map keys are not variable dependencies. Available parameter binders reach the checker's existing environment-shadowing refusal.

Parameter schemas are mapped lazily through the same invocation-local `SchemaTypes` and saved-contract resolver as forms. The full `Variable`, including nested `ValueBound`, reaches the existing Boolean-result checker. A successful variable or unresolved mapping status is cached once per invocation; neither an exception nor its traceback is retained. This also prevents shared malformed schemas from repeating their partial mapping work after the mapper clears failed memo entries. Response-only expressions do not depend on the parameter declaration.

Malformed or missing required schema targets preserve `SchemaError` incompleteness. Legal recursive closures and pure reference cycles preserve the existing mapper, including its dynamic result. There is no blanket cycle refusal, remote reference retrieval, consumer-declared substitute input type or synthetic schema for absent context.

## Verification and ownership

The implementation plan and actual independent design conclusions are recorded in [the issue](https://github.com/realkarych/kora/issues/640#issuecomment-6601177070). Final implementation review, tested revision, complete CI and any failed checks are recorded in the pull request; design review is not executable evidence.

| Obligation | Owning regression |
|---|---|
| Complete sibling/nested types, numeric kinds, enums, optional guards, arrays, unknown identifiers and shadowing; root/stored scope and unchanged author data | `test_declared_approval_parameters_use_complete_schema` |
| Input dependency in either order or dead branch never becomes complete | `test_declared_approval_parameters_do_not_complete_input_context` |
| Required malformed or unavailable schemas remain incomplete while independent response-only checks continue | `test_declared_approval_parameter_schema_failure_is_scoped` |
| Local form/parameter reference roots remain separate; saved references use existing closure | `test_declared_approval_parameters_keep_separate_reference_roots` |
| Empty and recursive schemas retain the accepted mapper behavior | `test_declared_approval_parameters_preserve_dynamic_and_recursive_mapping` |
| Full parameter bounds reach existing cost admission and preserve IR027; unchanged mapper supplies nested maxItems/maxLength bounds | `test_declared_approval_parameter_bounds_reach_cost_check` distinguishes maxItems admission; `test_cel_schema_bounds_follow_max_items_and_max_length` owns the lower-layer nested bound derivation |
| Successful and failed shared parameter mapping occurs once per call; effective wide metadata is exercised | `test_approval_parameter_mapping_is_once_per_invocation` |
| Deep single-allOf mapping does not grow the Python call stack | `test_approval_parameter_deep_mapping_is_iterative` |
| Caller parameter data, mapper/types/bounds, AST and environments are released while the result survives | `test_approval_parameter_context_releases_bound_state` and existing response ownership cases |
| DEBUG/stdout content exclusion, bounded blocked/broken output and actual writer shutdown | `test_explicit_approval_logging_excludes_content`, `test_explicit_approval_survives_unavailable_sink` |
| No optional binding or server dependencies enter the installed core validation path | `test_type_schema_validation_works_from_installed_core_only_wheel` |

The mapper, schema cache and source references have one invocation owner. Checkers and estimates remain per predicate; results retain only copied bounded diagnostics and scalar counts. The parameter cache is not shared across calls or used as execution authority.

For repeated predicates, successful and failed parameter mapping is constant in predicate count for the fixed schema. Checker work can still revisit reachable type/bound metadata per predicate. Instrumented step counts and exact owned-object/weak-reference checks establish their named work and release properties, not a wall-time, process-wide heap/RSS or whole-validator scale guarantee. No new numerical acceptance method or measurement campaign is introduced.

Security remains within TM-04/TM-10 for authored schema/expression work, TM-06/TM-07 for content-free diagnostics and TM-12 for truthful partial completeness. Actors, permissions, network authority and runtime trust assumptions are unchanged.

## Remaining activation

This partial result does not complete #552 or activate public validation. Effective input contracts, selected producer paths, stored-body forwarding, AgentSession policy, previous-attempt declarations, complete environments and binding compilation retain their existing owners. Whole graph validation, real runtime/harness facts, frozen execution contracts, deployment, canonical starts and approval transitions remain separate implementation work.

The existing public function and result names/fields remain unchanged. Scoped completeness describes only the eligible predicates this library pass can check; no API, scheduler or execution consumer may treat it as completed graph admission.
