# IR control configuration model evidence

Date: 2026-10-23. Scope: [issue #225](https://github.com/realkarych/kora/issues/225), following the common execution configuration models in [#212](https://github.com/realkarych/kora/issues/212). The [approved plan](https://github.com/realkarych/kora/issues/225#issuecomment-6354370510) defines this implementation slice; this record supplies evidence, not a new behavioral decision.

## Structural boundary

`BranchCase`, `BranchConfig`, `FanOutConfig`, `SubgraphNodeConfig` and `Retry` extend the existing closed IR records. Omitted optional values stay absent, explicit nulls fail, ordered arrays retain their order and branch targets may repeat. Retry arrays reject duplicates after enum conversion; overlap between the two arrays remains valid. Every error class is structurally admitted. Context-specific retry eligibility, reference resolution, branch ownership, expression checking and execution remain semantic work.

The branch's `else` key has the Python attribute `else_`. Shared record parsing accepts only declared wire names; it rejects the Python alias spelling, ambiguous dual spelling, internal `extensions`, unknown fields and non-string keys. Exports use `by_alias=True` and `exclude_unset=True`. Existing common records and `ToolPolicy` retain their fields and extension rules.

Expression values preserve raw text and enforce only the schema's 1–4,096 Unicode-code-point bound. Numeric fields retain JSON integer semantics and refuse booleans and numeric strings; backoff accepts finite numbers of at least one and preserves large Python/JSON integers. Pydantic's inherited JSON floating-point decoding limits remain unchanged.

Exactly four committed patterns replace the dollar end anchor with the portable `(?![\s\S])` assertion: common IDs, branch case targets, branch else targets and input sources. Their spelling and reserved alternatives remain unchanged. Final line feeds are refused, including after 64-character IDs and reserved values. Other committed patterns are unchanged.

## Error and observability boundary

The existing test-support projection masks every error-location segment and uses a fixed message, preserving the error count and type. Markers in expressions, targets, unknown keys, nested extensions and retry entries do not enter this projected result. It does not provide a configuration-to-public-`Diagnostic` adapter or field provenance. Shared diagnostic values, public protocol errors and the diagnostic measurement protocol are unchanged.

These data records own no process, logger, timer, connection or runtime slot. Compiler and validator callers own eventual diagnostics and operational events. Their implementing issues activate those checks. This slice adds no CEL compilation, graph scheduling or frontend interaction.

## Verification and measurement method

`test_ir_control_models_preserve_omission_and_wire_aliases`, `test_ir_control_models_enforce_structural_boundaries`, `test_ir_control_errors_exclude_input_content` and the existing schema/example suites cover the structural boundary. Generated fragments match the committed definitions in validation and serialization modes, including the inline branch-case definition and alias-backed field. Actual example traversal includes control configurations, graph/body/node retries and infrastructure retries.

A shared 242-case scalar corpus was checked with pinned `check-jsonschema` tooling and Node against committed and applicable generated fragments. The Python schema checker also exercised 126 containing-schema cases spanning all 18 references to the common ID definition. The PR retains the probe source, corpus and command results; these checks do not establish whole-graph semantic validity.

The registered IR suite preserves its existing 75 checks and adds 45 control-model checks. Minimal and populated round trips use three warm-ups and 30 timing samples. Independently scaled branch cases and targets use 1, 1,000 and 10,000 entries, with validation, export, final-entry refusal and many-error conversion/release; scaling and refusal workloads retain three warm-ups and ten samples. The else alias has a 10,000-entry workload. Retry workloads include every distinct enum and 10,000 invalid or duplicate entries. Two independently bounded case extensions exercise nested validation/export and a final oversize/deep refusal.

The configuration lifetime workers run three independent processes, each with 1,000 success/refusal cycles and quiescent checkpoints every 250 cycles. The inventory grows from six to eleven record types and includes populated else targets. Weak references check root and nested model release; retained heap, allocation differences, threads, descriptors and current/peak RSS retain their existing measurement method and gates. The separate upstream diagnostic workers are unchanged. The PR records exact final revision, environment, raw artifacts, base comparison and independent review conclusions. Timing and peak/native-memory observations establish baselines without a new numerical gate. The 1,000-node materialized graph remains with [#218](https://github.com/realkarych/kora/issues/218).
