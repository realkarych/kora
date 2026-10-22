# Shared diagnostics

Contract approved under [issue #207](https://github.com/realkarych/kora/issues/207), including the [exact-integer amendment](https://github.com/realkarych/kora/issues/207#issuecomment-6353301443), on 2026-10-23. This specifies values and consumer responsibilities; it does not establish that the compiler, validator, providers or API implement them. The remaining HTTP envelopes, statuses, authorization and transport failures belong to [issue #135](https://github.com/realkarych/kora/issues/135).

## 1. Representation and ownership

`kora.ir.diagnostics` owns the shared frozen `Diagnostic` and `SourceLocation` values, also exported by `kora.ir`. The value types, aliases and explicit `from_cel` conversion are implemented under [issue #219](https://github.com/realkarych/kora/issues/219), as a separately approved foundation before the validator and the policy/harness part of [issue #156](https://github.com/realkarych/kora/issues/156). It adds no package or import permission: `protocols` may import `ir`, and `ir` may import `cel`, under [ADR-0026](../../docs/decisions/0026-package-layers.md).

Every diagnostic has the following fields. `code` and `message` are required. Every other field is nullable and is present as JSON `null` when absent. There is no open extension or arbitrary-details object.

| Field | Representation | Meaning and bound |
|---|---|---|
| `code` | string | Extensible ASCII identifier matching `[A-Za-z][A-Za-z0-9_.-]{0,63}`. Existing IR and DSL codes retain their meanings; provider codes are declared by their installed contract. This is not a closed enum. |
| `message` | string | Reviewed fixed message for the code/reason, at most 1,024 UTF-8 bytes. |
| `path` | tuple of strings and nonnegative integers; JSON array, or null | Segments relative to the checked document or provider configuration. At most 64 segments; each string at most 256 UTF-8 bytes. Null means no known field; an empty array means the document root. String `"0"` and integer `0` remain distinct. |
| `location` | `SourceLocation {file, line}`, or null | Caller-relative source label, at most 1,024 UTF-8 bytes, and positive one-based source line. No absolute host path or URL. The compiler preserves its collected source information here. |
| `offset` | nonnegative integer, or null | Unicode code-point offset in the decoded expression/path field value. It is neither a YAML/source column nor a UTF-16 index. |
| `node` | string, or null | Accepted node identifier metadata, at most 256 UTF-8 bytes. |
| `hint` | string, or null | Reviewed fixed recovery hint, at most 512 UTF-8 bytes. |

All integers in this contract, including path indexes, source lines, offsets and collection counts, are at most **9,007,199,254,740,991 (2^53 − 1)**. Booleans are not integer indexes. This preserves exact ordinary JSON decoding in Python and TypeScript without a bigint wire format. Strings contain valid Unicode text; lone surrogates are refused. Byte bounds count UTF-8, not code points or UTF-16 units.

`ConfigWarning` and `ContractViolation` are public aliases of `Diagnostic`, with no additional fields or duplicate model classes. The enclosing result owns severity: `HarnessAdapter.check_config` returns warnings (`IR008` for fields it cannot honour), and `Policy.validate` returns violations (`IR013` for invalid policy). A code does not universally determine severity: `IR009` can be a warning or an error. The existing namespace-policy `IR019` and node-policy `IR008` distinction remains unchanged.

An example finding, separate from any HTTP error envelope:

```json
{
  "code": "IR003",
  "message": "Output and input types are incompatible.",
  "path": ["graph", "nodes", "review", "inputs", "run"],
  "location": {"file": "graphs/sql.py", "line": 19},
  "offset": null,
  "node": "review",
  "hint": "Check the output and input declarations."
}
```

The shared value has no HTTP status, request ID, retryability, authority or severity field. Runtime identifiers, request correlation and replay belong to enclosing contracts. A structurally valid diagnostic is not proof that an arbitrary producer's prose or metadata is safe.

## 2. Safe construction and location fidelity

Producers select messages and hints from reviewed fixed text. They never interpolate raw model input, configuration values, expression text, arbitrary keys, exception text, SDK messages, stack/locals, credentials or URLs. A recovery hint does not paste a command containing user values. The [observability content contract](observability.md#5-safe-content-and-trust-boundaries) applies at every level, including DEBUG; returned diagnostic bodies are not copied into operational logs.

Path segments may contain reviewed kora/provider schema fields or explicitly accepted identifier metadata. Names from author-supplied JSON Schemas, including properties, definitions, references and enum values, remain untrusted after schema validation. They do not enter a generic set of safe schema-known names. Unknown input keys and content-bearing map keys use the existing `<field>` marker. Expression tokens never become path segments. Nonsecret payloads need the same protection as secrets.

A producer unable to expose a source label, path or node safely and within its bound uses null, or the nearest safe ancestor path, while retaining the error code. It does not truncate a string into a different apparent identifier. Server validation does not invent source coordinates or open a file named by diagnostic metadata. A source label or node grants no filesystem or execution authority.

A nonnull `offset` requires the **exact expression field** to remain identified by the final safe path. Safety filtering, ancestor fallback or a rebase exceeding the path bound clears the offset when that identity is lost. Ordinary location-preserving conversion retains the original offset, including after non-BMP characters. Source line numbers and expression offsets are independent coordinates.

A provider returns a path relative to the configuration it checked. Its caller adds the known document prefix before global ordering and applies the same final-path bounds and offset invariant. It preserves safe source location, node and hint metadata. Renderers may display a YAML-style path; they never parse that display string back into canonical segments. Clients preserve segment kinds, nulls, source labels, offsets and producer ordering. A UI may derive a local highlighting index without changing the wire offset.

Two existing representations remain distinct:

- `kora.cel.diagnostics.Diagnostic` is the internal CEL `reason`/`offset` value. CEL does not import `ir`. The IR consumer explicitly maps `reason.code.value`, the reviewed fixed message, the caller's safe path and the unchanged offset into the shared value, subject to the final-path invariant. This does not implement schema-driven checking, projections or whole-IR validation.
- `kora.ir.errors.SafeFieldError` remains the existing narrow validation intermediate. `safe_location` retains its filtering role; its safe-name input must follow the provenance rule above. Public conversion uses `errors(include_input=False, include_context=False)` and reviewed messages, never raw Pydantic input/context, arbitrary `loc`/`msg` text or `str(error)`.

Consumers retain unknown well-formed codes without changing them to a known code or inventing semantics. The enclosing error/warning collection remains authoritative. Messages are for explanation, never branching or permissions.

## 3. Collection ownership and bounds

The validator collects every independent diagnostic it can establish for input within its accepted request bounds. A failed prerequisite prevents dependent semantic checks, but does not stop unrelated checks or cause fabricated secondary findings. The existing one-diagnostic-per-expression rule remains. Oversized or syntactically unreadable input has no all-diagnostics completeness promise.

The enclosing collection owns severity, ordering, retained bounds and omitted counts. None becomes a per-diagnostic field. Sort within each severity by canonical path segments, then offset, code, source file/line, node, message and hint, with stable producer traversal as the final tie breaker. Null sorts before a present value, including the root path; paths use lexicographic segment order, shorter prefixes first, integers before strings, integers numerically and strings by Unicode code point. Rebase provider paths before this ordering.

Compile, validate and deploy collections retain at most **1,000 errors** and **1,000 warnings**, with a combined diagnostic-data limit of **1 MiB**. Count the compact UTF-8 JSON bytes with no ASCII escaping or insignificant whitespace and fixed keys in `{"errors":[],"warnings":[]}`: arrays, keys and punctuation count; `valid` and omission counters do not. First select the first 1,000 entries of each sorted severity, then retain the longest prefix of errors followed by warnings that fits the byte limit. Never skip a larger earlier item to keep a later one.

Return separate `errors_omitted` and `warnings_omitted` counts, including both count and byte exclusions. `valid` is false whenever any error was found, even if the retained error array is empty. Count overflow beyond 2^53 − 1 is a content-free collection failure, never valid success. Clients preserve the returned order and counts instead of recomputing them from the displayed sample or JavaScript string lengths.

Collection retains a bounded ordered prefix while counting emitted diagnostics, evicting largest retained entries as earlier entries arrive. It does not first build an unbounded list and then truncate it. Request and parsed-document limits remain separate. The 1 MiB output cap is **not** a bound on total validation memory: the first validator consumer inventories schema/Pydantic error production and safe-conversion intermediates, including the existing eager `errors()` call, and bounds them incrementally or measures them under the maximum accepted input. Dense all-invalid input and repeated refusal cleanup are required workloads. These checks belong to work item 3, not a speculative validator in the typed foundation.

Provider `check_config` and `validate` lists contain at most **1,000 diagnostics** and **1 MiB** of compact UTF-8 encoded array bytes. The provider enforces both limits before appending the next diagnostic. Overflow raises `KoraProtocolError` with the fixed code `diagnostics_limit`, safe fixed text and no payload details; it returns no partial success list. The caller treats this as incomplete validation and fails closed, never warning-only success or `valid: true`. The enclosing failure mapping belongs to #135. These bound conforming provider work; they do not isolate hostile installed code.

## 4. Surface integration and verification

[DSL compilation](dsl.md#2-compile-step-kora-compile), [IR validation](ir.md#10-validation-rules-kora-validate), [harness configuration](protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) and [policy validation](protocols.md#12-policy-dd-29) consume the same representation. [API error envelopes](api-and-agent-surface.md#structured-errors-fr-04), statuses and client transport failures are separate contracts. The CLI, MCP and generated TypeScript consumers preserve diagnostics without moving validation or permissions into clients. Product harness references teach document path/offset and optional source location together, in the user's own session.

The typed foundation implements the shared-value portions of the first three checks below, together with `test_shared_diagnostic_bounds` and the diagnostic workloads in `tests/measurements/test_ir_models_cost.py`. Compiler, schema-provenance, provider, collection and API integrations remain required future checks; the foundation does not establish their implementation.

| Contract | Required evidence | Owner and activation |
|---|---|---|
| `test_shared_diagnostics_preserve_source_and_expression_offsets` | Source file/line, node and hint survive; CEL conversion preserves a non-BMP code-point offset when the exact field survives and clears it when that identity is lost; null and root paths remain distinct. | Shared-value conversion: #219; compiler integration when implemented. |
| `test_shared_diagnostics_exclude_input_content` | Rejected input, arbitrary exception text, unknown keys and nonsecret payloads do not enter public diagnostics or logs; author-schema names remain filtered even when identical names in reviewed schemas are allowed. | Typed foundation's boundary cases: #219; work item 3's schema/provenance conversion and work item 11's public errors. |
| `test_shared_diagnostic_codes_are_extensible` | Unknown well-formed codes survive; malformed codes refuse; aliases share the representation without deciding severity. | Typed foundation: #219; API/client preservation at work item 11 and generated-client activation. |
| `test_diagnostic_collections_are_bounded_and_count_omissions` | Deterministic count/UTF-8 prefix limits, separate omission counts, incomplete validation failing closed and `valid: false` despite omitted errors; upstream error-memory workload measured separately. | Work item 3; validate/deploy integration in work item 11. |
| `test_provider_diagnostics_rebase_without_losing_location` | Harness warnings and policy violations preserve safe metadata on rebase, clear offsets if exact identity is lost, refuse an oversized first item or 1,001st item, and cannot permit deploy after overflow. | Protocol consumers in #156/work item 6; real providers and work items 3 and 11. |
| `test_kora_compile_and_validate_report_same_location` | Both paths report the same code, segmented path and offset for kora parser/checker/cost failures, including a non-BMP character. | Work items 3 and 11; compiler leg pending compiler implementation. |

The implementation issues assume their component's observability and resource evidence before code. The typed foundation adds no process, socket, task, sink, registry or cache; callers own its values. Its validation/encoding/allocation and repeated failure-release measurements still apply. The active CEL compact and lifetime checks, existing shell smoke, database and scanner gates remain unchanged. A design review or named future test is not a passing executable check.
