# Output-schema consistency evidence

Date: 2026-10-30. Implementing [issue #479](https://github.com/realkarych/kora/issues/479), a scoped part of MVP work item 3. Actual commands, tested revisions, CI artifacts and independent results belong to the PR.

## Delivered boundary

`validate_output_schemas` accepts a structurally validated, caller-owned `GraphDocument` that remains unchanged during the synchronous call. It visits root and stored bodies once, including unused bodies, without expanding call instances. Only agent and human-input output declarations enter the new checks. Existing structural admission owns their fixed session/response roles.

Agents admit at most one non-list StructuredOutput-family output and one exact VcsRef. Saved custom parents establish family membership only through an available published built-in revision. Lists are not stripped; VcsRef descendants do not become exact VcsRef roles. Per-call memoization stores known and unknown classifications for every visited prefix; active-path membership detects cycles without repeated prefix scans. `ancestry_steps` counts each first map lookup, including missing entries. Unknown ancestry suppresses only dependent StructuredOutput findings; declaration and schema passes own prerequisite failures. Exact VcsRef and human-input checks remain independent.

Known structured outputs are compared only when both the output schema and agent configuration schema are authored. An explicit human-input response schema uses the authored form, or the published default when the form is absent. Empty maps remain custom. A fresh-value factory supplies both default-form comparison and the existing schema metadata, without changing generated schema bytes or inserting fields into documents.

Paired JSON traversal ignores object order and preserves array order. JSON kinds distinguish booleans from numbers; integers and finite floats use exact numeric-value comparison without float conversion of integers. References, annotations and dialect fields remain literal data. The comparator neither resolves references nor validates schema syntax, normalizes data or claims schema equivalence. It stops at the first mismatch. One iterator per active container avoids recursion and eager child copies; object membership checks and numeric/string sizes remain part of actual work beyond the value-pair counter.

Scoped validity does not establish prerequisite validity, inherited-schema compatibility, edge assignability, payload validation, approval defaults/predicates at execution, CEL environments, providers or public admission. No API/CLI/MCP operation, persisted format, dependency, platform or gate changes.

## Blocking evidence and ownership

`tests/ir/test_output_schemas.py` covers exact and inherited roles versus lists/unrelated outputs, matching/mismatching authored schemas, omission versus empty maps, unknown ancestry beside independent findings, root/shared/unused bodies and all eight node kinds. The shared-ancestry cases include 1,000 agents and 1,000 parent links with valid, missing, cyclic and unpublished endings. Export assertions preserve authored absence; default-form copy mutation cannot affect later calls. Existing IR-schema generation tests pin unchanged published bytes.

`tests/ir/test_schema_equality.py` covers JSON kinds, numeric equality and precision boundaries, huge integers, nested object order, array order, literal references/annotations, depth 5,000 and width 10,000. A wide first-child mismatch counts two compared values; source inspection additionally checks lazy iterator ownership, because that counter alone does not bound pending work or memory.

Mismatch diagnostics identify the explicit output schema; cardinality diagnostics identify the output map. Neither includes a differing authored key, value or reference. Node source is revalidated without ancestor substitution. Deep private markers remain excluded; 3,000 independent mismatches preserve 1,000 retained errors and 2,000 omissions. Existing collector regressions retain the combined 1 MiB obligation.

`test_output_schema_validation_releases_inputs_state_and_results` retains results while weak references prove document, graph, agent/human nodes, configs, declarations, schema roots, source values and saved contracts release after success and refusal. Temporary checker/ancestry objects are gone after collection. Dropping results releases their diagnostics and reconstructed source values. No task, thread, descriptor, transaction or subprocess belongs to this synchronous pass; its state has no cross-call cache.

The existing isolated installed-core-wheel check imports the new pass and performs an actual human-input comparison, alongside saved/inline schema checks. `test_output_schema_logging_is_content_free` excludes authored schema/reference/source markers and stdout at DEBUG configuration. `test_output_schema_result_survives_unavailable_sink` preserves results with real blocked/broken sinks, checks existing queue bounds and releases the writer under unchanged shutdown deadlines. Completion emits validity and counts only.

## Diagnostic method and limits

`test_output_schema_resource_observations` uses existing Report/Sample and JUnit property `output_schema_resources`: 1/100/1,000 matching human-input nodes, depth 5,000, width 10,000, 1,000 agents sharing 1,000 parent links, 1,000 calls of one stored body, and 3,000 mismatches. Input construction is excluded. Each workload receives one warm-up and three untraced timings: 24 samples. Six mixed batches record post-GC current traced heap and cumulative peak since tracing began, with available before/after RSS. Peaks are not reset between batches. macOS RSS is unavailable through the collector; shared-host load limits timing comparisons.

Exact node/body/output/ancestry/comparison/value counts, diagnostic omissions/bounds and actual resource release are blocking. Time, process-wide heap and RSS are diagnostics without thresholds, convergence claims or leak-freedom guarantees. Iterators scale with active depth, ancestry memoization with visited names, and the unchanged collector with retained diagnostics. Keys, strings and number sizes also contribute work. No absolute input/time/memory bound or cancellation guarantee is introduced.

Independent correctness, security and performance profiles cover the shared IR output/schema boundary and form metadata consumer. Unchanged collector/logging transport, databases, providers, scheduler and public clients stop propagation. A demonstrated contract defect, retained owned resource or material unexplained growth triggers bounded investigation; unchanged diagnostics do not add campaigns or merge gates.
