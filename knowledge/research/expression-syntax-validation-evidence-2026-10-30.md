# Expression and path syntax validation evidence

Date: 2026-10-30. Implementing [issue #487](https://github.com/realkarych/kora/issues/487), a scoped server part of MVP work item 3. Actual commands, revisions, native CI artifacts and independent results belong to the PR.

## Delivered boundary

`validate_expression_syntax` accepts a structurally validated, caller-owned `GraphDocument` that remains unchanged during its synchronous call. It directly uses the existing pure CEL parser and path parser. It does not invoke canonical rendering, schema typing, cost estimation, the native evaluator, a provider or a resolver.

Operational CEL sites are branch case conditions/reasons and else reasons; every node retry predicate; stored-body retry predicates; human-input approval predicates; event matches; wait/trigger sensor predicates; and runtime infrastructure retry predicates. Disabled and dormant policies are still checked. Operational paths are every node input path, agent/command workspace references, human-input context and binding paths including each gate digest, and output-target paths. Empty permitted path strings are parsed; absent fields stay absent. Opaque metadata, schema/config payloads, commands and prompt templates are not recursively interpreted.

Root and every stored body are visited once, including unused bodies. Calls do not expand instances. Counters identify all visited nodes, all stored definitions, all authored triggers including non-sensors, and every expression/path parser invocation including failures. A lazy field iterator retains only the current enumeration state. The parser owns one field's temporary tokens/tree; no accumulated field list, AST cache or input-bearing result is introduced.

Scoped syntax validity can coexist with unknown names, wrong contextual roots, wrong result types or failures in other validator passes. It establishes no approval defaults, type environment, rendering agreement, binding compilation, cost admission, path resolution, installation support or execution readiness. Structural admission already enforces the 4,096-code-point field limit; tests do not bypass it.

## Blocking evidence and ownership

`test_expression_syntax_covers_operational_fields` and `test_expression_syntax_covers_path_fields` cover every node kind's common fields and every kind-specific field in root and unused-body scopes. Document/body/trigger/target tests protect each source owner. A disabled infrastructure retry still parses its predicate; every approval binding item gets its own indexed path. A 1,000-call shared body containing an actual predicate is parsed once.

Malformed punctuation, unsupported constructs, macros, literal regex constraints and static depth limits retain existing IR011/IR027 diagnostics. Path tests cover operators/calls, negative/hex/unsigned/dynamic indexes, reserved identifiers and exact depth-refusal offsets, including Unicode code-point accounting. Syntactically valid unknown names, wrong roots and wrong result types explicitly protect the deferred semantic boundary. Complete export comparisons protect authored absence and unchanged values.

Each independent invalid field contributes one existing fixed message and its exact field/offset. Node fields use safe node source; stored-body retries use body source; sensor-trigger/target paths use graph source with no node; runtime predicates have neither. Unsafe source labels are omitted without ancestor fallback. Dense short errors retain 1,000 findings and count 2,000 omissions. A separate structurally admitted long-name/source case reaches the shared byte cap before the count cap.

Success/refusal lifetime tests retain results while weak references prove graph/body/node/config/source release. They compare live AST/path counts before and after the call and assert no parser, path parser, syntax checker or field record survives collection. Dropping the result releases its copied diagnostics and source. These checks protect owned Python objects; they are not a process-wide native leak guarantee.

`test_expression_syntax_logging_is_content_free` uses unregistered content markers in expressions, paths and source with DEBUG enabled. `test_expression_syntax_result_survives_unavailable_sink` exercises real blocked/broken pipes, unchanged results, existing queue caps and actual writer shutdown. The syntax pass owns no thread, task, descriptor, subprocess or database; the unchanged logging transport owns its writer. The installed-core-wheel test executes a real invalid predicate and verifies its IR011 and offset.

## Diagnostic method and limits

`test_expression_syntax_resource_observations` records existing Report/Sample data in JUnit property `expression_syntax_resources`. Seven workloads cover 1/100/1,000 node predicates, 1,000 input paths, 1,000 calls of one body, 100 predicates at the existing length boundary and 3,000 syntax failures. Each receives one warm-up and three untraced timings, giving 21 samples. Six mixed post-GC batches record current traced heap and cumulative peak since tracing starts. Input construction and traced timing are excluded. RSS is unavailable on macOS; shared-host and observation overhead remain limitations.

Exact scope/counts, static parser limits, diagnostic caps and owned-object/writer release are blocking. Time, heap and RSS have no threshold, latency-distribution, convergence, NFR-3 scheduling or general leak-freedom claim. Aggregate work still scales with the stored document and field lengths. A retained owner, demonstrated contract defect or material unexplained growth triggers bounded investigation; diagnostic variation does not add a repeated campaign.

Independent correctness, security and performance profiles cover the common enumeration/parser/diagnostic boundary and package integration. The unchanged parser language, collector/logging contracts, databases, providers, scheduler and public clients stop propagation. No new dependency, execution policy or verification gate is introduced.
