# Common IR execution configuration models

Checked on 2026-10-23 for [issue #212](https://github.com/realkarych/kora/issues/212). The [approved plan](https://github.com/realkarych/kora/issues/212#issuecomment-6353479077) defines this structural subset; the [IR specification](../specs/ir.md) and [schema](../specs/ir.schema.json) remain authoritative.

## Delivered boundary

`kora.ir.config` supplies `NodeLimits`, `Workspace`, `NetworkPolicy`, `McpServer`, `CommandResource` and `CommandConfig`. `kora.ir.record` shares closed-record and flattened `x-` extension handling with `ToolPolicy`; `kora.ir.scalars` supplies the existing duration, digest and environment-name patterns. Neither the bounded JSON parser nor the safe-error converter changes. The protocol layer continues using its existing configuration boundary.

Optional fields use the installed Pydantic missing sentinel: omission remains absent on export, explicit null is refused, and no execution or deployment default is inserted. The locked environment used here contains Pydantic 2.14.1 and pydantic-core 2.50.1. Integral JSON numbers are accepted by integer fields without admitting booleans or numeric strings. Positive budgets retain integers without float conversion. Python and committed JSON Schema patterns use absolute-end assertions for durations, digests, environment names and resource names. The resource traversal lookahead scans every character, including Unicode line separators. These rules preserve the specified complete-string grammars across Python and JavaScript without trimming or normalizing author input.

Numeric validation operates on decoded Python integers and finite floats. Integral floats convert to integers without further numerical loss. Raw JSON decoding retains Pydantic's floating-point precision and range behavior; these models do not preserve arbitrary-precision decimal tokens. For example, decoding `1.0000000000000001` yields `1.0` before field validation, and `1e-400` underflows to zero. Any future exact-token guarantee needs an explicit graph-loading and serialization contract; this subset does not change the established JSON value representation or protocol boundary.

The shared serializer flattens extensions. Its JSON-schema hook uses the model's validation fields for serialization schemas, retaining the generator's local-reference registry. Command environment maps explicitly publish their key constraint as `propertyNames`: the default patterned-key emission describes a different acceptance rule. Fragment tests inline only the named local references and discard annotations, preserving every validation constraint and refusing unexpected reference siblings.

MCP environment and header keys remain arbitrary strings; MCP secret-map values use alias syntax. Command environment keys and both sides of its secret map use that syntax. A key beginning `x-` inside a named map is an ordinary entry. MCP transport fields do not gain conditional requirements or destination validation.

Command resources check the committed lexical pattern, digest spelling, size and count bounds. They do not establish Unicode normalization, uniqueness, manifest integrity, safe materialization or permission to execute. Each nested record has its own existing extension limit of 1 MiB and depth 16; there is no aggregate configuration limit. Frozen records do not imply deep immutability. Content and raw Pydantic errors remain unsuitable for operational diagnostics; callers use the existing fixed-message, allowlisted-location conversion.

## Executable evidence

`tests/ir/test_ir_models_match_schema_definitions.py` compares validation and serialization fragments, including all three network-policy occurrences. Example tests traverse graph and nested-body nodes; they do not confuse provenance or runtime provider configuration with node configuration. `tests/ir/test_ir_config_models.py` covers omission, explicit empty values, numeric boundaries, required fields, unknown fields, map distinctions, extension composition and content-free error conversion. Existing ToolPolicy and architecture checks remain required.

The [approved lexical amendment](https://github.com/realkarych/kora/issues/212#issuecomment-6353868121) repairs final-LF acceptance in Python and traversal after U+2028/U+2029 in JavaScript. `test_ir_config_models_reject_trailing_control_characters` covers the actual constrained consumers in Python and JSON input modes, including safe errors. `test_ir_resource_names_preserve_unicode_segments` retains valid Unicode segments while refusing traversal, dot and empty segments; unrestricted MCP map keys remain unchanged. A shared 52-case corpus passed against the committed fragments and both generated schema modes using check-jsonschema 0.38.2 tooling, and against the committed patterns using Node 24.22.0. Twenty additional schema cases cover valid and refused values through retry, timer, approval, wait, sensor, webhook, runtime, agent, provenance and secret-alias references. These are structural grammar checks, not evidence of implemented consumers.

The registered `tests/measurements/test_ir_models_cost.py` command preserves the original 17 checks and adds 51 configuration/lifetime cases, including four lexical regression workloads. Each new lexical workload uses 10,000 digits or path segments, three warm-ups and ten samples: final-LF duration and path refusal, valid Unicode path segments, and Unicode-separated traversal refusal. On macOS arm64 with CPython 3.11.17, the initial complete run passed 64 checks in 24.24 seconds, recording 993 timing samples and 73 retention gates. All gates passed. These are local baseline observations, not new time or native-memory thresholds; final-revision CI artifacts and the PR supply release evidence.

| Local workload | Median per operation | p95 per operation | Traced peak |
|---|---:|---:|---:|
| Populated command, 64 resources, validate/export/drop | 0.268 ms | 0.301 ms | 132,285 bytes |
| Command containers, 10,000 entries, validate | 2.879 ms | 2.964 ms | 659,512 bytes |
| Command containers, 10,000 entries, export | 2.508 ms | 2.567 ms | 3,588,591 bytes |
| MCP containers, 10,000 entries, validate | 1.617 ms | 1.716 ms | 889,080 bytes |
| 64 independently bounded resource extensions, validate | 36.589 ms | 37.041 ms | 825,248 bytes |
| Same extensions, export | 3.336 ms | 3.421 ms | 1,449,730 bytes |
| Command argv with 10,000 errors, safe conversion/drop | 11.952 ms | 34.241 ms | 8,844,266 bytes |
| Resource path, 10,000 segments, validate | 0.349 ms | 0.359 ms | 22,654 bytes |

The nested-extension input represents 66,853,184 canonical bytes across 64 records. Its repeated immutable string leaves are shared by the fixture, so traced allocations do not claim to include an independent copy of every payload byte. Fixtures and export models exist before each measurement baseline. Ordinary cases use three warm-ups and 30 samples; scaling/refusal cases use three warm-ups and ten samples. Every original and new operation retained fewer than 65,536 traced bytes after release; the largest local operation delta was 80 bytes.

Three fresh processes each performed 1,000 cycles containing a populated success and a refused input for every model. At all 12 checkpoints, weak references showed zero surviving model instances, and each process returned to its baseline of one thread and four descriptors. The final traced delta was 11,220 bytes in each process, below the unchanged 65,536-byte gate. Allocation differences identify measurement-result storage and tracing/subprocess support. Current RSS rose by about 0.6–0.9 MiB from the warmed baseline and then approached a plateau; current/peak RSS, allocation differences and all four checkpoints are retained in the report. macOS current RSS uses `ps` in KiB converted to bytes; Linux uses resident pages from `/proc/self/statm`. `getrusage` peak units are normalized to bytes. Each worker's successful process exit is checked.

An archive of base revision `893d07c0e98c36bc96eb9345471df0fb6684bdd2` ran the unchanged measurement module using the same interpreter, locked dependencies and local runner. Its 17 checks passed in 13.59 seconds. Head/base median ratios were 1.009 for 250,000-pattern validation, 1.000 for export, 1.000 for depth-16 extension validation, 1.008 for dense extension validation and 1.000 for repeated refusals. This comparison provides no evidence of a material shared-path regression; the new models have no executable base and establish their first baseline.

## Ownership and limitations

These synchronous records own disposable model/collection allocations and intentional process-lifetime Pydantic class schemas. They create no connections, registrations, asynchronous tasks or execution subprocesses. Cancellation, recovery and runtime shutdown behavior remains with future consumers; measurement workers are test infrastructure whose exit is checked.

Threats TM-04, TM-06/TM-07, TM-10 and TM-11 inform the negative tests, without claiming runtime containment, secret authorization, resource integrity or log-emitter activation. Compiler/validator diagnostics activate with those consumers. The remaining graph model, AgentConfig decisions, whole-graph schema and 1,000-node materialized-graph workload belong to [issue #218](https://github.com/realkarych/kora/issues/218). No CEL budget, import permission, dependency, scanner, CI timeout or existing measurement gate changes.
