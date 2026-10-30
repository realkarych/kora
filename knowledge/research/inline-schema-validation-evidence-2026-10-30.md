# Inline graph-schema validation evidence

Date: 2026-10-30. Implementing [issue #476](https://github.com/realkarych/kora/issues/476), a scoped part of MVP work item 3. Actual commands, tested revisions, CI artifacts and independent review results belong to the PR.

## Delivered boundary

`validate_inline_schemas` accepts a structurally validated, caller-owned `GraphDocument` that remains unchanged during the synchronous call. It checks graph parameters, every node output schema, stored subgraph input schemas, agent output schemas and human-input forms. Every stored body is visited, including unused bodies, once rather than once per call instance. No schema-looking data in triggers, annotations or extensions becomes an additional root. Missing fields remain missing; an authored empty map remains a checked schema. Missing inline `$schema` selects draft 2020-12 without inserting a field.

The shared per-root checker retains the existing [saved-schema admission, native projections, regex handling and offline pointer policy](type-schema-validation-evidence-2026-10-30.md). There is one native keyword validator per call. Each inline field is the root of its local references. Only supplied saved contracts create artifact URNs; root `$id` creates no alias, and inline roots cannot publish one another. The cache key includes the live root object's identity and reference string. Caller document ownership keeps inline roots alive, and the per-call saved map keeps packaged built-in roots alive, until the cache is released. No ephemeral copied root enters this cache.

Unknown built-in pairs remain errors in the saved-type pass. The inline pass excludes them from available targets and reports only actual unresolved references. An unrelated malformed saved schema or parent is not an inline-schema failure. Thus `inline_schemas_valid` cannot establish saved-contract validity, effective output/form agreement, approval defaults or predicates, inherited compatibility, behavior binding, edge assignability, CEL typing, payload validity or execution admission. No public operation, dependency, platform, gate or persisted format changes.

## Blocking evidence and ownership

`test_inline_schemas_check_every_node_output` covers all eight kinds in the root and unused bodies. The node-configuration and graph/body-owner tests cover the remaining fields and exact diagnostic paths, node IDs and source ownership. Unsafe source labels are omitted without replacing them with another owner's source. The absence/empty-map and dialect tests check scoped validity and unchanged export. Payload/metadata and approval-semantics regressions preserve the result's limits.

`test_inline_reference_cache_isolated_by_root_in_both_orders` pairs identical local reference strings with different valid/invalid roots, in both insertion orders. The offline-position corpus checks all three reference keywords, escaped pointers, false schemas, canonical indices, invalid UTF-8, anchors, nested resources, data positions and external references. Further cases cover cycles, every supplied built-in and a custom URN, missing saved contracts, inline ID alias/override attempts and unavailable built-ins. Shared-body counts show that 1,000 call sites do not multiply body validation.

The existing saved-type corpus remains a regression for the extracted checker and resolver changes. The existing installed-core-wheel test now imports both passes and resolves an inline built-in reference through the packaged resources, retaining offline installation and import isolation. The package resource byte checks remain unchanged.

`test_inline_schemas_bound_deep_independent_faults_and_mask_authored_names` combines 1,200 nested schema positions, regex capacity refusal and 3,000 invalid alternatives. Fixed `IR022`, masked map keys, the 64-segment ancestor fallback and existing count/byte collector bounds remain blocking. Independent faults survive refusal; omissions cannot produce success. The existing collector tests preserve the combined-byte obligation.

`test_inline_schema_validation_releases_inputs_views_and_results` retains results while weak references prove document, graph, inline root and original source release after success and refusal. Safe container projections are gone after collection; dropping the result releases its diagnostics and reconstructed source values. No task, thread, descriptor, transaction or subprocess belongs to this synchronous validator. Existing transport owns logging shutdown and failures.

`test_inline_schema_logging_is_content_free` runs completion logging at DEBUG configuration while excluding authored schema/reference/source markers and stdout. `test_inline_schema_result_survives_unavailable_sink` uses real blocked/broken sinks, preserves results, checks queue bounds and releases the writer under existing shutdown deadlines. The event contains validity and counts only.

## Diagnostic method and limits

`test_inline_schema_resource_observations` records the existing Report/Sample structure in JUnit property `inline_schema_resources`: 1/100/1,000 inline roots, depth 1,200, 1,000 repeated local references, 1,000 calls of one stored body, and 3,000 invalid alternatives. Each workload receives one warm-up and three untraced timings. Six mixed batches record post-GC current/peak traced heap and available RSS. Input construction and traced timing are excluded. All samples and environment metadata remain visible; macOS RSS is unavailable through this collector. Shared-host load limits timing comparisons.

Exact node/body/root/schema/reference counts, resource release and returned diagnostic bounds are blocking. Elapsed time, process-wide heap and RSS are diagnostic, without thresholds or a convergence/leak-freedom claim. Work includes schema and reference characters; the reference cache and traversal state scale with supplied input and die with the call. The existing dependency-owned regex cache is entry-bounded, not byte-bounded. No new absolute input, regex time/space or interruption guarantee is introduced.

The verification plan groups inline enumeration, shared schema validation, reference ownership and diagnostic conversion under IR validation. Independent correctness, security and performance profiles cover this common boundary. Unchanged transport, databases, providers, scheduler and public clients stop propagation. Material unexplained growth, retained component resources or a demonstrated contract defect triggers bounded investigation; unchanged diagnostic outliers do not add campaigns or merge gates.
