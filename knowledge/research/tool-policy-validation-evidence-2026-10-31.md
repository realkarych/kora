# Static tool-policy validation evidence

Date: 2026-10-31. Implements [issue #518](https://github.com/realkarych/kora/issues/518), a scoped part of MVP work item 3. Revisions, commands, actual results, review provenance and CI artifacts belong to the PR.

## Delivered boundary and shared ownership

`kora.ir.tool_patterns` owns the existing pure tool-pattern compiler, its constants, `ToolPattern`, fixed refusal reasons and lazy array/layer refusal enumeration. Its inputs remain `ToolPolicy`, strings and existing glob primitives. It imports no provider, protocol, registry, filesystem resolver or evaluator. The exact lexical edit names remain Edit, Write, MultiEdit, NotebookEdit and apply_patch. The provider retains its unchanged harness-to-tool table; a regression checks that its current edit names agree with the shared lexical set.

The provider imports the shared grammar in its default mode, preserving original validation messages, relative paths, null layer roots, document parsing and diagnostic-limit behavior. `Layer`, runtime matching, layer precedence, protected-path resolution, output-channel runtime exception, fuel and the prepared-cache lifetime remain provider/consumer owned. Moving internal compiler types changes no provider protocol ABI or persisted representation.

`tool_policy_refusals(policy)` is the kora-owned pure layer checker, also suitable for a future namespace-policy caller. It selects exact `StructuredOutput`-name refusal after ordinary successful compilation. Both Name and Name(argument) forms are refused in allow/deny; wildcard, case-distinct and qualified names retain ordinary grammar. Malformed reserved-name patterns receive their existing grammar refusal once. Provider `validate` retains its original grammar-only behavior; the accepted universal reserved-name rule belongs to kora independently of provider validation. No namespace state, provisioning, permissions or consumer is introduced.

`validate_tool_policies(GraphDocument)` assumes structural validity and a caller-owned snapshot unchanged during the synchronous call. It visits root and every stored body once, including unused/shared definitions, without invocation expansion. Only authored `AgentNode.config.tool_policy` values participate; omission remains omission, an explicit empty policy is checked, and opaque extension/schema/command data is not interpreted. Each eligible pattern is compiled once by the selected grammar enumeration. Oversized arrays emit an array refusal and do not compile their members; the other array and independent aggregate checks continue.

`ToolPolicyResult` retains bounded copied diagnostics and nodes/definitions/policies/patterns counters. Patterns count all authored allow/deny entries, including entries in arrays over their admitted bound; the counter is not a machine-operation count. `tool_policies_valid` covers grammar, existing capacities and reserved-name refusal only. It proves neither runtime enforcement nor full installation/public admission.

## Existing bounds and diagnostic ownership

Existing limits remain inclusive: 4,096 pattern code points; 128 entries and 65,536 bytes per array; 96 characters for searched pieces in exact/case-folded forms; 8,192 raw wildcard pieces and 16,384 non-ASCII characters per layer. Pattern-array bytes preserve `globs.utf8_size`, the sum of raw pattern UTF-8 with the existing surrogatepass behavior, without JSON punctuation or a new Unicode admission rule. Consecutive stars retain the existing compiler collapse and original aggregate-piece counting. Deny combining-mark refusal and exact allow behavior are unchanged.

Every document finding is fixed IR013 beneath the owning node's `config.tool_policy`: array limit at allow/deny, malformed/unsupported/reserved pattern at its array index, aggregate limit at the policy root. Only that node's revalidated source survives; no body/graph fallback. Node identity is retained, offset is null, and pattern/configuration contents are never interpolated. Existing shared diagnostic sorting/count/byte limits and omission semantics apply. Provider violations retain their separate relative representation and diagnostics_limit envelope.

## Blocking evidence

| Boundary | Named evidence |
|---|---|
| Grammar, argument/name/Unicode forms and inclusive capacities | `test_tool_policy_patterns_follow_shared_grammar`, `test_tool_policy_bounds_preserve_inclusive_limits`, allow-mark/wildcard and oversized-array tests |
| Universal output-channel rule versus unchanged provider behavior | `test_tool_policy_output_channel_is_kora_owned`, `test_malformed_reserved_patterns_have_one_grammar_diagnostic`, `test_reserved_output_rule_keeps_provider_validation_unchanged`, shared grammar equality tests |
| Runtime compatibility and edit-name ownership | Existing `tests/plugins/policy/` plus concrete policy/shared conformance suites; `test_lexical_edit_names_match_unchanged_runtime_table` |
| Stored scope, omissions and unchanged input | `test_tool_policies_visit_root_unused_and_shared_definitions_once`, `test_tool_policy_pass_preserves_omission_empty_and_opaque_fields` |
| Safe bounded output | `test_tool_policy_diagnostics_use_only_safe_node_source`, separate diagnostic count/byte-bound tests |
| Exact temporary/input/result release | `test_tool_policy_validation_releases_call_state`, `test_namespace_reusable_core_checker_releases_the_supplied_layer` |
| Logging and installed package | `test_tool_policy_logging_is_content_free`, `test_tool_policy_result_survives_unavailable_sink`, `test_type_schema_validation_works_from_installed_core_only_wheel` |

The synchronous call owns current generator/compiler/folded strings and the bounded collector until return; no static cross-call cache, task, thread, descriptor, process or database resource is added. Results retain no graph/body/agent/config/policy/original-source/compiled-pattern object. Weak references establish release while results live, then copied diagnostic/source release when results drop. The standalone core checker similarly releases its input layer after complete enumeration. Synchronous return and actual unchanged logging-writer shutdown cover applicable failure/shutdown paths; this introduces no interruption guarantee.

INFO `ir.tool_policies.completed` in component `ir.validation` carries only validity, total error/omission counts and traversal counters. Core helpers emit no event, leaving one reporting owner. DEBUG-configured markers in patterns, extensions, node names and source labels stay out of logs/stdout. Real blocked/broken pipes preserve validation results, queue bounds and actual writer closure. Process entry points retain unexpected-failure reporting.

## Diagnostic resource method and limits

`test_tool_policy_resource_observations` emits the existing Report/Sample format as JUnit property `tool_policy_resources`. Ten workloads cover 1/100/1,000 valid policy nodes; 1,000 entries sharing one agent/config/policy model; both full 128-entry arrays; an exact 64 KiB array; combining-mark refusal; 3,000 malformed patterns across nodes; oversized-array refusal; and aggregate-piece refusal. One warm-up and three untraced calls per workload yield 30 samples. Six mixed post-GC batches record settled traced heap and cumulative peak since tracing began, plus RSS when available; macOS RSS is unavailable. Construction/tracing are excluded from timings, local checks are serialized, and shared-host/runtime/report allocations limit attribution.

Work includes visited stored nodes, array byte sizing, eligible pattern compilation/folding and aggregate scans over authored text. Shared model identity does not deduplicate distinct node declarations. No unique-text-only work or universal operation quota is claimed. Exact existing capacities, diagnostic bounds and owned-state release remain blocking. Elapsed time, process heap/RSS and peaks are diagnostics without thresholds, convergence/leak-freedom or operational-envelope guarantees. Demonstrated retention, contract defects or material unexplained growth trigger bounded investigation; diagnostic outliers alone add no campaign or merge gate.

## Trust and activation limits

Existing TM-04/TM-10 controls cover hostile authored grammar and bounded compilation/output, TM-06/TM-07 fixed content-free diagnostics, and TM-03 the preserved output-channel/runtime residual. No actor, authority, credential, external integration, persistence, retention or control changes; no threat-model amendment is required. Independent security review checks that mapping against the actual diff.

No tool call is matched or executed by the new static pass. Protected paths, IR019/IR028 coverage, adapter/provider admission, namespace provisioning, secrets, CEL environments, filesystem resolution and HTTP/CLI/MCP operations retain their owners. The graph is not declared runnable by a scoped valid result. No dependency, platform, package import permission or numerical gate changes. Existing documentation remains accurate; no docs/ status-only edit is needed.

Planning inspected the retained successful master audit 41025696626, source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`: fresh partial-budget, 165 executed, zero reused, 70 killed, 18 survived, 77 test errors and 315 pending. Only pricing/CEL paths are declared; it supplies no policy finding or additional PR mutation gate.
