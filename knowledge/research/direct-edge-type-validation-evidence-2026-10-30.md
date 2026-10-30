# Direct-edge nominal type validation evidence

Date: 2026-10-30. Implementing [issue #482](https://github.com/realkarych/kora/issues/482), a scoped server part of MVP work item 3. Actual commands, revisions, native CI artifacts and independent results belong to the PR.

## Delivered boundary

`validate_edge_types` accepts a structurally validated, caller-owned `GraphDocument` that remains unchanged during the synchronous call. Every node input in the root and every stored body is considered once, including unused bodies. Calls do not expand instances, and ordinary names resolve only inside the owning body.

An authored path, including `path: ""`, any reserved source or a list consumer excludes the input first. Otherwise missing producer/output references are unresolved, followed by exclusion of a resolved list producer. Remaining non-list endpoint names must both belong to the valid saved ancestry index. Only then is an edge checked: the producer must be the expected type or its saved descendant. Optional input presence does not waive declared compatibility. Checked, unresolved and excluded counts partition all visited inputs.

The per-call index examines each saved contract once. Published built-in name/revision pairs and namespace-qualified custom null-parent entries are roots. Qualified parent edges form reverse adjacency lists. Because each contract has at most one parent, every component reachable from a valid root is a tree; missing/cyclic/unqualified/unpublished components and their descendants remain unreachable. Iterative depth-first traversal assigns half-open intervals using a global preorder counter across all roots. Different trees therefore cannot overlap. The active traversal stack contains one iterator per depth, not an eager sibling frontier. Indexed endpoint comparison uses interval containment without a parent walk or ancestor-set closure.

The index owns only per-call adjacency, stack and name/interval values. The result retains only frozen counts and bounded copied diagnostics. `contracts_checked` counts supplied contract-map entries and `types_indexed` counts reachable names; these counters do not count name characters, dictionary hashing or every machine operation.

`edge_types_valid` establishes only nominal compatibility for checked edges. It can coexist with invalid link/declaration/schema passes and excluded inputs. It does not establish inherited CEL field compatibility, schema subsumption, behavior availability, payload validity, path/reserved input typing, list variance or aggregate activation, body-interface/export/collect typing, public API/CLI/MCP admission or execution readiness. No schema, provider or current registry lookup occurs; no authored field changes.

## Blocking evidence and ownership

`test_direct_edge_types_follow_saved_ancestry_direction` covers identical custom/builtin types, multi-level descendants, reversed direction, siblings and unrelated roots in root and unused-body scopes. Every node kind participates as both producer and consumer. Scope tests distinguish omitted paths from authored empty/nonempty paths, every reserved source, valid/deep/malformed list wrappers, optional inputs, missing source/output/type and body-local name lookup. A shared body with a real edge is visited once despite 1,000 calls.

`test_ancestry_intervals_match_independent_many_root_forest_oracle` checks every pair against a separate parent-chain oracle across a branching many-root forest in two declaration orders. Deep 10,000-entry ancestry, every published builtin root, invalid components and same-name invalid comparisons protect interval boundaries and unknown propagation. Many distinct edges share one deep index; no repeated-query cache is needed. Source inspection checks the linear adjacency/index and lazy traversal separately from counters.

Unknown prerequisites beside an independent known mismatch preserve the latter's IR003 and the existing link/declaration pass's findings. A schema/behavior test demonstrates that nominal success does not evaluate schema contents or activate a missing provider. Export assertions preserve input data and authored absence.

Mismatch diagnostics use fixed text at the consumer input type. Only accepted node/input identifiers enter paths; type/schema/reference/behavior values are not interpolated. Consumer source is revalidated, and an unsafe source is omitted even when graph/producer source is safe. The 3,000-mismatch case retains 1,000 errors with 2,000 omissions. Existing collector regressions preserve the combined 1 MiB obligation and deterministic selection.

`test_edge_type_validation_releases_inputs_index_and_results` retains a result while weak references prove document, graph, body, nodes, input/output declarations, contracts, schemas and original source release after success and refusal. No temporary ancestry/checker object survives collection. Dropping the result releases its copied diagnostics and source values. The pass owns no thread, task, descriptor, transaction, subprocess or cross-call cache; synchronous return is its release boundary.

`test_edge_type_logging_is_content_free` uses unregistered nonsecret markers in type names, schema/reference content and source with DEBUG configured, checks the count-only INFO event and stdout exclusion. `test_edge_type_result_survives_unavailable_sink` uses real blocked/broken sinks, unchanged queue limits and actual writer shutdown. The isolated installed-core-wheel test performs an actual direct-edge comparison alongside the existing schema passes.

## Diagnostic method and limits

`test_edge_type_resource_observations` records existing Report/Sample data in JUnit property `edge_type_resources`. Eight workloads cover 1/100/1,000 matching edges; depth 5,000 with 1,000 distinct ancestor comparisons; 1,000 siblings; 1,000 edges sharing invalid depth-1,000 ancestry; 1,000 calls of one two-node body; and 3,000 mismatches. Each workload receives one warm-up and three untraced timings, yielding 24 samples. Six mixed post-GC batches record current traced heap and cumulative peak since tracing began. Input construction and traced timing are excluded. RSS is unavailable on macOS; shared-host load and measurement reporting remain visible limitations.

Exact scope/traversal counts, bounded diagnostics and actual input/index/result release are blocking. Time, process-wide heap and RSS are diagnostic without thresholds, a latency-distribution claim or general convergence/leak-freedom guarantee. Index storage scales with supplied contracts and traversal depth; diagnostic storage is bounded by the existing collector. Input/name size still contributes to work, and no absolute time/memory quota or interruption guarantee is added.

Independent correctness, security and performance profiles cover the common saved-ancestry/direct-edge/diagnostic boundary and package integration. Unchanged collector/logging transport, databases, providers, scheduler and clients stop propagation. A demonstrated contract defect, retained owned resource or material unexplained growth triggers bounded investigation. Unchanged diagnostic outliers do not require another campaign or merge gate.
