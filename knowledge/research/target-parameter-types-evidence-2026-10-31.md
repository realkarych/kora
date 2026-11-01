# Known declared target-parameter type evidence

Checked 2026-10-31. Owner: [issue #554](https://github.com/realkarych/kora/issues/554), [pre-code implementation/verification plan](https://github.com/realkarych/kora/issues/554#issuecomment-6538424074), base `822433f3ea9dadb0862bafadf4492c3b54a4147b`. Fresh separate [correctness/security design results](https://github.com/realkarych/kora/issues/554#issuecomment-6538457145) passed the exact plan without a new normative choice before source edits. Design coverage is not implementation evidence or owner approval.

## Scope and unchanged owners

`validate_known_target_parameter_types` owns only the exact declared property of an authored root ParameterTarget parameters schema mapped to a PLAIN Record. STRING primitives include enum/const metadata. Other known non-Dyn types are IR020; Nullable is never unwrapped, and optional string declarations remain legal. No target and OutputTarget have zero obligations.

Missing authored schema, SchemaError, non-Record root, missing property and Dyn remain unresolved with no secondary diagnostic. Exact Record.fields lookup neither borrows additional/pattern properties nor chooses parameter existence, schema defaults or runtime values. Scoped success requires valid diagnostics and zero unresolved. Full IR020 composition remains work item 3; public aggregation remains work item 11. First-slice graph.target IR028, complete target-key value/regex checks and ADR-0019/0020 runtime/admission/binding/history ownership remain unchanged. Shared selected-provider design #549 is unrelated and remains unimplemented here.

One validation-local mapper/resolver/meter uses the existing saved-contract catalogue. It opens no authored URI or source path, calls no provider/native binding and retains no global cache. Returned diagnostics are reconstructed by the existing bounded collector; parameter names/types/schema/references never enter their fixed path/message or scalar completion event. Only revalidated owning graph source survives, with null node/offset/hint.

## Evidence and limits

| Obligation | Owning evidence |
|---|---|
| Exact known declared type, optional/nullable and enum/const behavior | `test_known_target_parameter_types_compare_only_declared_properties` |
| Existing real local/saved/built-in refs and single-allOf semantics | `test_known_target_parameter_types_preserve_mapping_rules` |
| Honest missing/open/dynamic/mapping-failed state; zero nonapplicable obligations | `test_known_target_parameter_types_leave_other_sources_unresolved` and other-target-form cases |
| No authored file/network reference authority | Missing/external/absolute reference cases and real filesystem-URI refusal with an existing test-owned file |
| Safe fixed IR020 and graph-only metadata | `test_known_target_parameter_types_keep_safe_location`, unrestricted/long/non-BMP parameter names and unsafe source labels |
| No activation or runtime-value inference | `test_known_target_types_keep_first_slice_refusal`; existing IR028 remains visible |
| Exact input preservation and release | `test_known_target_parameter_types_release_inputs`, success/refusal/unresolved: borrowed document/graph/target/schema/contract/source weak references disappear while the result survives; validation-local mapper/Record counts return to baseline; result/diagnostic locations release after disposal |
| Safe completion and failure-independent logging | `test_target_parameter_type_logging_is_content_free` and `test_target_parameter_type_result_survives_unavailable_sink`: unregistered markers at DEBUG, authorized context/reset/stdout exclusion, real blocked/broken pipes, bounded queue and actual writer shutdown |

Local CPython 3.11.17, macOS 27.2 arm64, task-private locked environment: **94 new cases passed** and **251 related schema-mapping/IR/architecture cases passed**, zero failures/errors/skips. Raw JUnit is `/tmp/kora-target-554-focused-final.xml` and `/tmp/kora-target-554-related.xml`. These local files lack embedded source identity; the PR records the coordinator's unchanged production/test input comparison to the committed revision. Whole Ruff format/lint, strict Pyright and edited-specification Markdown checks passed. Actual implementation C/S results and one complete final-head Linux/macOS CI remain required in the PR; no source-review request substitutes for a returned result or executable checks.

The initial focused run preserved 82 passes and 11 fixture-construction failures in `/tmp/kora-target-554-focused.xml`: saved schemas omitted their required dialect, and surrogate ParameterTarget text failed existing structural admission. Corrected saved fixtures retain the dialect; valid non-BMP text exercises the semantic privacy case, while a separate negative test preserves the structural surrogate refusal. No production assertion or admission rule was relaxed. Initial fixture kwargs/type annotations and a long test name were corrected before the final strict run.

The pass acquires no process, database, task, provider or sink; existing infrastructure owns collection and logging queue/writer. Existing TM-04/06/07/10 cover hostile schema/content and resource ownership; TM-08/11 authority limits remain unchanged. No new threat-model actor/control, installed provider entry point, package resource, API/CLI/MCP operation, dependency, layer permission, persisted format, platform, limit, timeout or verification gate is introduced. Existing packaging and supported-environment CI remain mandatory; no separate installed-provider case is activated. Schema steps are existing counted work, not an absolute time/memory guarantee. No new performance method or campaign is selected.
