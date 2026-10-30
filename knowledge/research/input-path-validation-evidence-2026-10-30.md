# Node-local input path validation evidence

Date: 2026-10-30. Implementing [issue #491](https://github.com/realkarych/kora/issues/491), a scoped server part of MVP work item 3. Commands, tested revisions, native CI artifacts and independent results belong to the PR.

## Delivered boundary

`validate_input_paths` accepts a structurally validated, caller-owned `GraphDocument` that stays unchanged during the synchronous call. Shared lazy enumeration selects agent/command workspace references and human-input context, VCS binding and each gate-digest binding. The existing pure path parser owns syntax and static limits. A successful parse must start with `Field("inputs")` and a field naming an input on the same node. Membership is exact, with dot and decoded string-index notation equivalent. No copy of an input map is made.

Root and every stored body, including unused bodies, are visited once without expanding calls. Body, caller and sibling declarations grant no visibility. Optional declarations are eligible. Syntactically valid suffixes are not walked or typed. Local membership does not establish valid producer references, a runtime value, an artifact type, a digest or approval authority. Relative input and graph-target paths remain outside this pass.

Fixed IR002 workspace/context and IR005 binding errors identify only the operational field, node ID and revalidated safe node source, with null offset. Unsafe source is omitted without ancestor fallback. Parser failures produce no duplicate syntax findings, increment unresolved paths and prevent scoped success. Counters satisfy `paths_checked == bindings_checked + paths_unresolved`; successful parses include semantic refusals. Independent fields continue after either kind of refusal. Results own only copied bounded diagnostics and scalar counts.

## Blocking evidence and ownership

`test_input_paths_bind_to_current_node` covers all five roles in root and unused-body scopes, dot/string indexes, decoded escapes, numeric suffixes, exact spelling, missing names, wrong/bare roots and integer second steps. Export comparisons protect unchanged authored fields. Scope tests cover absent/empty local maps when body, caller or sibling inputs have matching names. Optional inputs, unresolved producer/type references and unverified suffixes protect the explicit deferred boundary.

`test_input_paths_do_not_duplicate_syntax_errors` combines an independent semantic fault with empty, malformed and depth-limited paths. Only the syntax pass emits IR011/IR027, while input binding remains non-success with exact counters. Multiple gate-digest entries preserve indexes. Source tests exclude unsafe labels and ancestor fallback. Dense 3,000-error tests enforce the 1,000-error count cap and a separate long-name/source fixture reaches the 1 MiB output cap, retaining omitted totals and excluding authored path contents.

`test_input_paths_release_owned_objects` exercises successful, semantically invalid and syntactically unresolved paths. It retains the result while weak references prove release of the graph, root, body, node, config, input declaration, binding and source. Live path counts return to their baseline, with no parser, checker or field record remaining. Dropping the result releases its copied diagnostics and source. These are owned-object checks, not process-wide leak guarantees.

`test_input_path_logging_is_content_free` enables DEBUG with unregistered markers in valid/refused paths and source labels. The INFO event contains only validity and counts; stdout remains empty. `test_input_path_result_survives_unavailable_sink` runs 2,000 validations against real blocked and broken pipes, checks unchanged results and existing record/byte limits, and confirms the writer has stopped after shutdown. The pass itself acquires no task, thread, descriptor, process or database. The existing transport owns logging resources.

The syntax field matrix remains a regression for shared enumeration and unchanged traversal. The installed core-only wheel program executes an undeclared approval VCS binding and verifies IR005, counts, null offset and content-free stdout.

## Diagnostic method and limits

`test_input_path_resource_observations` records existing Report/Sample data in JUnit property `input_path_resources`. Six workloads cover 1/100/1,000 human context paths, 1,000 calls to one body containing one binding, 100 paths at the existing 4,096-code-point boundary and 3,000 semantic failures. One warm-up and three untraced timings per workload give 18 samples. Six mixed post-GC batches record settled traced heap and cumulative peaks since tracing starts. Input construction and traced timing are excluded. RSS is unavailable on macOS; observer bookkeeping and shared-host variation remain limitations.

Exact traversal counts, existing parser/collector limits and owned-object/writer release are blocking. Elapsed time and process-wide heap/RSS are diagnostics without thresholds or latency, convergence, scheduling or general leak-freedom guarantees. Work scales with stored nodes/bodies and path lengths, plus bounded collector ordering. A retained owner, demonstrated contract defect or material unexplained growth triggers bounded investigation; diagnostic variation does not add a repeated campaign.

Independent correctness, security and performance profiles cover the shared enumeration/parser/membership/diagnostic boundary and package integration. Unchanged parser language, schema/provider resolution, evaluator, databases, scheduler and public consumers stop propagation. No dependency, layer allowance, execution policy, persisted format or verification gate changes.
