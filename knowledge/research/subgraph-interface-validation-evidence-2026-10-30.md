# Ordinary subgraph interface validation evidence

Date: 2026-10-30. Implements [issue #505](https://github.com/realkarych/kora/issues/505), a scoped part of MVP work item 3. Exact revisions, commands, independent results and CI artifacts belong to the PR.

## Approved contract and delivered boundary

The project owner selected complete identical input/output name sets, wrapper-input assignability to body inputs and exact output-type preservation on 2026-10-30. [IR section 4.4](../specs/ir.md#44-fan-out-and-subgraph) records that decision. `validate_subgraph_interfaces` accepts a structurally validated `GraphDocument` whose caller-owned maps remain unchanged during the synchronous call.

Root and every stored body are scanned once, including unused/shared and recursive definitions. Only ordinary `subgraph` nodes compare interfaces. Calls do not expand. Omitted/empty maps denote empty interfaces; optional bindings still declare names. Matching input types use the wrapper as produced and the body as expected, through saved ancestry and equal list depth. Matching output types require valid spelling/ancestry on both sides before exact equality. Neither widening nor narrowing is accepted. Internal producer types do not refine the declared body export.

Existing `graph_links` owns extra wrapper outputs as IR002. They are unresolved here, without duplicate errors. This pass owns missing outputs, missing/extra inputs and known type mismatches as fixed IR003 at the wrapper member or its `type` child. Missing body definitions, malformed type spellings and unavailable saved ancestry remain unresolved; independent known comparisons continue. A body export's missing producer does not erase its declared interface. Only the wrapper node's source is revalidated, with no graph/body/producer fallback; node ID is retained and semantic offset is null.

`SubgraphInterfaceResult` retains bounded copied diagnostics and scalar counters: nodes, definitions, ordinary wrappers, missing-body wrappers, contracts, indexed types, input/output union members, compared types, unresolved types and owned name mismatches. Each resolved wrapper contributes each name in the respective wrapper/body map union once. Always `inputs_checked + outputs_checked == types_compared + types_unresolved + names_mismatched`. Missing-body wrappers contribute no interface member counts. `interfaces_valid` means no established mismatch owned by this pass; extra outputs alone can coexist with true while graph-link validation fails. It is neither complete graph validity nor execution admission.

## Blocking evidence and ownership

| Boundary | Named evidence | Ownership |
|---|---|---|
| Input direction and exact output preservation | `test_subgraph_input_types_follow_saved_ancestry`, `test_subgraph_outputs_preserve_declared_export_type`, `test_subgraph_output_uses_export_interface_instead_of_internal_producer`, `test_subgraph_interfaces_compare_list_depth_without_recursion` | IR pass → unchanged saved ancestry/parser; correctness and security. |
| Name completeness and diagnostic ownership | `test_subgraph_names_preserve_complete_sets_and_diagnostic_ownership`, `test_extra_output_alone_remains_graph_link_owned`, `test_absent_and_empty_subgraph_interfaces_are_equivalent`, prerequisite and missing-link tests | New IR pass plus unchanged graph-link/declaration/export boundaries; correctness. |
| Stored traversal and exclusions | `test_subgraph_interfaces_visit_root_unused_shared_and_nested_wrappers_once`, `test_subgraph_interfaces_exclude_fanout_and_opaque_schema_binding_metadata` | IR synchronous call; correctness/performance. |
| Safe bounded diagnostics | `test_subgraph_interface_diagnostics_use_only_safe_wrapper_source`, `test_dense_subgraph_interface_errors_preserve_count_bound`, `test_subgraph_interface_errors_preserve_byte_bound` | Existing collector owns copied output; correctness/security/performance. |
| Exact release | `test_subgraph_interface_validation_releases_call_state` covers success, type/name refusal, unknown/malformed type, missing body and extra-output prerequisite | Per-call index/checker/parser/lookup state; correctness/performance. |
| Logging and wheel integration | `test_subgraph_interface_logging_is_content_free`, `test_subgraph_interface_result_survives_unavailable_sink`, `test_type_schema_validation_works_from_installed_core_only_wheel` | IR event; unchanged transport queue/writer and installed package; correctness/security/performance. |

The call owns its ancestry index, current type shapes/base strings, interface lookup locals and bounded collector until return. Results retain no document, body, wrapper, declaration, contract, schema or ancestry object. Weak references establish release while results remain alive, then copied diagnostic/source release when results drop. There is no new cache, task, thread, descriptor, database transaction or child process. Synchronous completion and the existing logging writer's actual shutdown cover applicable failure/shutdown paths; this adds no interruption guarantee.

INFO `ir.subgraph_interfaces.completed` in `ir.validation` carries only fixed validity/error/omission/counter fields. DEBUG-configured markers in authored schema/type/node/source values remain absent from logs and stdout. Real blocked/broken sinks retain existing queue bounds and cannot change validation results. The process entry point owns unexpected-failure reporting.

## Complexity and diagnostic method

Work includes one saved ancestry index, stored nodes/definitions and each resolved wrapper's actual and expected interface maps. Shared body declarations must be compared separately for each wrapper because wrappers may differ. Every matching endpoint spelling is parsed, including repeated spellings; no unique-name-only work claim is made. Ancestry lookup is constant time after indexing. Parsing/indexing and traversal are iterative, without a new list-depth cap. Diagnostic sorting/encoding uses the existing retained bounds; temporary storage comprises the index, current parser/lookup state and collector.

`test_subgraph_interface_resource_observations` uses the existing Report/Sample format in JUnit property `subgraph_interface_resources`: 1/100/1,000 wrappers, 1,000 depth-two wrappers, 1,000 wrappers sharing depth-5,000 ancestry, 1,000 wrappers with invalid ancestry, 1,000 unused bodies, 100 depth-2,000 wrapper types and 3,000 type refusals. One warm-up plus three untraced calls per workload yields 27 samples. Six mixed post-GC batches record settled traced heap, cumulative peak since tracing began and RSS when available; macOS RSS is unavailable. Construction and tracing are excluded from timing. Local observations run apart from other local checks; hosted contention and runtime/report allocations limit attribution.

Time, process heap/RSS and peaks are diagnostics without pass thresholds, convergence claims, throughput/latency guarantees or general leak-freedom. Exact counters, bounded output and actual owned-state release remain blocking. Demonstrated contract defects, retained owned state or material unexplained growth trigger bounded investigation; diagnostic outliers alone add no merge campaign.

## Trust and activation limits

Existing threat-model controls TM-04, TM-06/TM-07 and TM-10 cover malformed authored declarations, safe content and bounded diagnostic/resource ownership. No actor, credential, authority, external integration, retention policy or control changes, so `docs/threat-model.md` needs no new control. Independent security design review confirmed that mapping; actual results and evidence remain separately reviewable.

No schema, behavior binding, current registry, provider, payload, input path, optional-binding value or `collect` metadata is interpreted. Fan-out input/over/collect, runtime expansion/order, effective-schema compatibility/evaluation, typed CEL, installation admission and HTTP/CLI/MCP endpoints remain separate. ADR-0027 and existing AgentSession/StructuredOutput/CommandResult aggregate-consumer gates remain active. Unchanged providers, database, scheduler and public consumers stop transitive review propagation.

The latest applicable master audit inspected during planning was run 41025696626, source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`: fresh partial-budget, 165 executed, zero reused, 70 killed, 18 survived, 77 test errors and 315 pending. Its declared CEL-path/pricing scope includes no interface component, so there is no applicable finding or additional PR mutation gate.
