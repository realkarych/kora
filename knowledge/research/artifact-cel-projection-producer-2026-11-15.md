# Detached StructuredOutput CEL projections

Date: 2026-11-15. Implementation: [#1637](https://github.com/realkarych/kora/issues/1637), following the [accepted representation](artifact-cel-projection-storage-2026-11-15.md). The normative owner and format remain [data-model projection storage](../specs/data-model.md#lossless-artifact-cel-projection-storage).

## Delivered boundary

`engine.artifact_projection` prepares and checks immutable projection bytes outside database transactions. Preparation accepts a complete `CheckedExecutionContract`, exact output path, original value and installed behavior bindings. It selects the saved v2 StructuredOutput closure, checker role and normalized paths, constructs the complete effective composition, validates the original, and only then cuts and converts that same model. The payload digest covers its full original canonical bytes, including unselected fields.

The prepared result retains only immutable binding/identity scalars and exact encoded bytes. The graph/version/output binding remains separate from the three semantic record digests: independently accepted owners can legitimately share identical content. Checking compares the externally supplied expected binding, representation, availability, length and complete digest before decoding the bounded byte record, then independently reconstructs its conversion and canonical bytes. Expected metadata is data supplied by a future owner, not an authorization certificate. Neither a legacy marker nor a standalone valid model can enter as a substitute for complete preparation.

The first delivery supports StructuredOutput only. No migration, actual derived owner, accepted-output transaction, retention operation, Worker report or scheduler grant is added. Native physical admission, old-row preceding fields, PostgreSQL TOAST, current authorization, atomic acceptance and uncertain commit recovery remain the real producers' obligations under #1472. Native transport tests persist test-owned BLOB/bytea values, never synthetic accepted artifact rows. Collection/export examples exercise original-value recomposition through the library; they activate no graph route.

## Finite temporary admission

The existing `cel_eval` layer owns pure view preparation, cutting/conversion and the byte codec; `engine` owns complete graph/output association and installed effective composition. Existing package allowlists and ADR-0026 remain unchanged. The source adapter reuses executable-reference selection, retaining inert references in the immutable general closure without executing their targets. Completed schema mismatch remains `validation`; schema budget refusal is `capacity`, and unsupported policy/behavior is `unavailable`.

`ProjectionLimits` separates source characters, variable data bytes, logical entries and work units. A logical entry is a fixed record or a container/index/frame slot; it is not a Python heap byte. Variable bytes count text, encoded buffers and numeric storage separately. The caller owns already admitted input/graph data and reserves these additional allowances before invoking the operation. Reservations accumulate conservatively across phases; later disposal does not refund this operation's allowance. No RSS, allocation-header, latency or concurrent-serving guarantee follows.

Let S be the combined lengths of the already checked canonical IR/contract strings; these encodings are ASCII. The selected caller path is separately included in the source admission. Let B = 8515, V = 256, J = 1048576 and C = 4096 + 32V. The following are conservative ownership reservations, not newly narrowed product value limits.

| Phase | Variable bytes | Logical entries | Work |
|---|---|---|---|
| Checked source selection, path reconstruction and checker view | `256S` plus the existing one-output preparation reservation when producing | `64S` plus that same combined preparation reservation when producing | `64S²` plus existing one-output preparation work; actual checker-meter work is charged in addition |
| Original model and full effective validation, producer only | `64J + max_variable_bytes` of the saved standard policy | `64J + max_auxiliary_entries` of that policy | `64J² + 4*max_validation_score` |
| Record parsing, conversion and canonical verification | `80B + 8C` | `16B + 64V` | `64B²` |

The source reservation includes the decoded whole contract, selected wrapper dictionaries, resolver names, type/bound memo/index/stack records, path lexer/trie/sort state and canonical path bytes. Source occurrences are bounded by S even when many other outputs are present. Schema mapping memoizes the selected original positions and references; it does not expand a reference tree. Path reconstruction can retain prefixes at up to 32 levels; four bytes per Unicode code point and simultaneous input/render/token ownership fit the `256S` text allowance. Whole-contract parsing and bounded decimal conversion, string comparisons/sorting and path work fit the quadratic work allowance. The existing output preparation reservation covers its source scans, closed-reference graph and role checks; charging its combined scratch amount in both dimensions over-reserves instead of conflating entries with bytes.

The model reservation covers original admission/snapshot, canonical encode/decode/check passes, the native view passed to installed validation, and subsequent canonical digest/cutter input. Container minimum-width checks precede copying even for an invalid oversized input. The standard policy's separate variable/entry ceilings reserve the complete numerical validation decision while those original owners remain live. Installed Python behavior is trusted cooperative code: its private allocations and unexpected exception chains are not bounded by schema or projection sizes. The real caller retains responsibility for that behavior and exception disposal.

The raw record's B-byte envelope bounds decoder occurrences and duplicate-pair state before parsing; it does not assume that a malformed record has only 2V values. The `16B` logical allowance separately covers decoded container rows/slots, temporary object-pair tuples/slots, duplicate checking and bounded traversal state. Text decoding, token strings, integer magnitude and scalar scratch fit `80B`; integer conversion is limited to 310 characters and float conversion to 32 characters before constructing those numbers. Lexical nesting is checked before JSON construction. Fixed records/keys and canonical identity checking are included, even on duplicate, Unicode or early refusal paths.

Conversion runs only after measuring the original's existing 4096-byte/256-value domain. It preserves topology and strings/keys, while each numeric token can grow by at most the reserved 32 characters; C therefore bounds an intermediate that may later exceed the successful converted-byte limit. The `64V` entries cover original and converted containers, traversal frames and measurement stacks. `8C` reserves conversion numeric data plus canonical text/UTF-8 buffers and concatenation intermediates. Successful encoding independently checks both values before constructing their canonical encodings; full-record re-encoding retains the incoming bytes and parsed original/converted data until exact comparison.

These conservative fixed-phase reservations intentionally exceed typical demand. The caller supplies its own aggregate capacity; they authorize no thread count or service capacity. Tests distinguish each resource dimension and show that an underfunded malformed decoder refuses for capacity before attempting JSON interpretation. A fully funded malformed record proceeds to its encoding refusal. The exact maximum record remains admissible, and an oversized converted value refuses independently of a valid original cut.

## Lifetime and diagnostics

This synchronous library creates no database unit, thread, pool, queue, global slot or cancellation owner. Expected failures return only a fixed enum from the inner frame; outer functions discard their input parameters before raising a context-free exception. Successful results retain neither full payload nor schemas. Existing real schema observers verify disposal after success and refusal. Unexpected trusted callback failures propagate with their traceback: the caller must retain capacity until its already accepted cleanup boundary finishes. Existing graph-preparation completion tests retain that owner's cancellation and cleanup coverage; this library does not claim new asynchronous closure evidence.

The caller-configured DEBUG event has only fixed operation/outcome fields. Tests cover content exclusion, native invalid bytes and blocked/broken diagnostics with actual writer shutdown. Diagnostic output never creates acceptance authority.

## Verification map

| Obligation | Owning checks |
|---|---|
| Original numeric kind, signed zero, scalar roots, Unicode and effective validation | `tests/engine/test_artifact_projection.py` |
| Exact native SQLite/PostgreSQL bytes | `test_cel_projection_number_kinds_round_trip` |
| Closed/canonical format, digest roles, malformed parser and independent size/count bounds | `tests/engine/test_artifact_projection_codec.py` |
| Frozen path digest vectors, equal-byte owner separation, original-value recomposition and actual CEL decisions | `tests/engine/test_artifact_projection_composition.py` plus existing `test_cel_projection_matches_full_binding` |
| Admission-before-use, expected disposal, trusted exceptions and diagnostics | `tests/engine/test_artifact_projection_lifetime.py`; existing graph-preparation completion regressions retain their original scope |

The latest applicable available master mutation audit inspected during planning was 43705847856 at `2dff3824df3b93d2a0937300219a3978c770cec5`, attempt 1, with no projection/converter finding. It supplies no additional qualification. Final-revision checks and actual independent correctness/security/performance results are recorded in the implementation PR; planned checks are not passing evidence.
