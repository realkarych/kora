# StructuredOutput equality and native-object indexes: scope and evidence

Date: 2026-10-31. Implementing issue: [#519](https://github.com/realkarych/kora/issues/519), coordinated by [#138](https://github.com/realkarych/kora/issues/138). Baseline: `9da36e868083ec47b03c685cb4f33334d79f92ed`. The [implementation plan](https://github.com/realkarych/kora/issues/519#issuecomment-6535100636) applies the complete policy accepted under #515. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3.3](../specs/artifacts/StructuredOutput.md#33-exact-bounded-evaluation) remain authoritative; this record adds no algorithm, capacity or execution authority.

## Internal interfaces and owners

The helpers live in existing-layer `kora.ir` modules and have no public package export. [`structured_schema_cursors.py`](../../src/kora/ir/structured_schema_cursors.py) owns the borrowed handles, exact visited-kind/indexed accessors, string primitive and native-object index. [`structured_schema_equality.py`](../../src/kora/ir/structured_schema_equality.py) owns iterative comparison.

| Internal interface | Input, result and ownership |
|---|---|
| `NativeCursor(value)`; `native_cursor(value, meter)` | Frozen borrowed native handle; the factory reserves its fixed row before construction. The referenced value is content-hidden and remains caller-owned. |
| `ArenaCursor(nodes, node)`; `arena_cursor(nodes, node, meter)` | Frozen borrowed handle into original `tuple[SchemaNode, ...]` and node ID, supplied by `FrozenSchemaArena.nodes` or `PreparedStructuredSchemaInput._arena`; the factory reserves its row. No reconstructed schema value is created. |
| `NativeObjectIndex(_rows, maximum_key)`; `build_native_object_index(mapping, meter)` | Call-owned encapsulated mutable sorted list of borrowed key/value rows and finite maximum-key-length metadata; sort scratch is temporary. |
| `NativeObjectIndex.row(position, meter)`; `.find(query, meter)` | Borrow an existing row, or return its sorted position/`-1` with the accepted binary-lookup charge. Neither operation copies values or performs authored-key hashing. |
| `cursor_kind`, `scalar_value`, `container_count`, `array_item`, `object_key`, `object_value` | Metered access to visited exact kinds, borrowed scalars, lengths and indexed children/members. Child descent creates a newly charged handle; reading a key or existing row does not copy it. |
| `string_equal(left, right, meter)` | One charged exact native string equality; no normalization or callbacks. |
| `json_equal(left: NativeCursor, right: NativeCursor \| ArenaCursor, meter: EvaluationMeter) -> bool` | Native/native or native/arena exact comparison, returning a completed boolean or propagating fixed refusal/unexpected error through the caller's existing boundary. It creates no meter, policy or acceptance value. |

The private `_PairFrame` has seven fields: `left`, `right`, `phase`, `position`, `count`, `left_index` and `right_index`. Its phases are `enter`, `array`, `keys` and `values`; position/count track only the current comparison. The frame owns its optional native indexes until unwind. The equality call owns the explicit stack and all frames. Child handles are constructed only on descent; arena member rows and original scalar values remain borrowed. There is no pending-child vector, recursive generator, schema/schema endpoint or global retainer. Existing `schema_equality.compare_json` remains the separate structural IR-schema comparator. New owner-record representations hide content, and `eq=False` prevents generated recursive dataclass equality.

## Caller admission and authority

For an eventual operation the caller obtains exactly one private native tree through `structured_output.checked_value(model)`. Every native comparison handle borrows that tree, a subtree or a scalar/key from it. The caller excludes concurrent mutation, retains that private tree for the call and never hands it to behavior that can mutate it. Handle construction neither admits a tree nor transfers ownership. The frozen model and canonical bytes remain the sole eventual extraction authority.

The existing model/raw boundary owns whole-domain exact-kind, Unicode, integer, byte, depth, cycle and duplicate-name admission. The prepared arena owns schema-literal admission and canonical unique member rows. Defensive exact-kind checks on visited values do not validate unvisited children: length, kind or key mismatch may complete before those children are reached. This helper adds no eager tree scan, identity set, payload arena or payload node limit. Two handles to the same admitted subtree still incur the prescribed comparison and independently owned native indexes; no identity discount or operation-wide cache is introduced.

## Accounting and deterministic traversal

All comparison, index and numerical operations share the caller's existing `EvaluationMeter`; they create no second policy or counters. Reservations precede actions and never refund. Outside the named bulk charges, every exact-kind/length read, logical row/field read or write, branch, iterator/index step, cursor advance and frame push/pop follows the accepted one-unit schedule. Fixed record creation and field initialization remain separate from bulk collection/sorting.

For an index with `n` members, total key code-point length `S` and maximum key length `M`, collection reserves exactly `2n` entries and `1+3n` work. Stable bottom-up mergesort reserves exactly `2n` scratch entries and `1+2p(n+S)` work, where `p=(max(1,n)-1).bit_length()`. Its named bulk covers internal comparisons, loops and copying; it does not cover surrounding headers or record initialization. Sorting borrows key/value references, uses one native `<=` per merge decision and returns the encapsulated sorted row list without a result-tuple copy. Preparation's implementation and acceptance are unchanged.

| Owned allocation | Cumulative auxiliary entries |
|---|---:|
| Each new native or arena handle | 1 |
| Equality stack | 1 header plus 1 cell per pushed pair |
| Each pair frame | 1 fixed row, separate from handles and stack cell |
| Native index and its row vector | 1 fixed row plus 1 vector header |
| Index collection | `2n`: `n` fixed key/value rows plus `n` row-vector cells |
| Merge scratch | 1 vector header plus exactly `2n` scratch cells |
| Borrowed arena/index row, scalar text/value or binary lookup | 0 new entries; ordinary work still applies |

A new descended pair therefore adds four entries for its two handles, frame and stack cell; caller-created root handles are not charged twice. An explicitly constructed native index adds three fixed entries and exactly `4n` variable entries, including its temporary scratch. Its empty vectors still have headers. Equality avoids index construction for empty objects and unequal member counts. Borrowed text and values create no auxiliary-byte copy reservation. These are logical policy counts, not Python object-size or heap/RSS measurements.

Sorted lookup costs 1 for an empty index. Otherwise it reserves `1+(h+1)*(1+len(query)+M)`, with `h=n.bit_length()`, before binary search and final equality. Exact native strings are required; there is no authored-key hash lookup. Equality traverses sorted row positions directly. A string equality reserves `1+min(len(left),len(right))` for exactly one native comparison. Numerical pairs call the unchanged `numeric_equal` implementation, retaining mandatory left/right conversion, zero tests, sign tests and only then alignment; boolean separation precedes numerical dispatch.

Arrays compare lengths before accessing items, then descend through paired indexes in order. Objects compare member counts first, build the left native index before the right when needed, compare every canonical sorted key pair, and only then compare corresponding values in canonical order. Arena members remain borrowed. The first completed mismatch stops comparison, so an expensive earlier value cannot hide a later key mismatch.

The explicit pair stack is limited by `MAX_EQUALITY_FRAMES=17`: an admitted native operand has at most 16 nested containers, followed by at most one current scalar pair. A deeper schema literal cannot add another matched native container. At least one operand is native; schema/schema equality is outside this endpoint. This proof is independent of the future 64 active schema-call limit and requires no Python recursion-limit change.

## Failure, disposal and activation limits

A completed boolean states only exact JSON equality. Expected unsupported visited kinds and exhausted reservations propagate the existing fixed `EvaluationRefused`, never schema-false. Representations and refusal outcomes contain no authored keys, values or native chain. Unexpected programming errors propagate to the existing caller-owned reporting boundary, which must not format payload locals or native exception chains.

Stack creation prepays final closure, and each push prepays its eventual pop. Unconditional unwind clears the stack on true, false, expected refusal and unexpected error without a new reservation that could replace the original outcome. Normal return releases the call's remaining local frame/index references; released state never refunds cumulative accounting. A caller-held exception or traceback can retain local frame/index state until disposal. Release evidence must distinguish that explicit retention from helper-owned global state and dispose the caught exception before final no-retainer assertions.

The helper creates no process, thread, task, descriptor, asynchronous interrupt or wall-time guarantee. It adds no per-comparison event or logging component; the eventual complete evaluator owns its one completion diagnostic. Full evaluation, every supported handler, safe pinned-engine adaptation, common composition and real consumers remain subject to their existing implementation and activation gates. The reserved v1 identity remains unavailable for effective execution. Provider authorization, original reference authority, saved identity, all thirteen parameters, preparation, numerical primitives and public surfaces are unchanged.

## Verification requirements and current status

The final focused command `PYTHONPATH=<isolated-worktree>/src uv run --no-sync pytest -q tests/ir/test_structured_schema_cursors.py tests/ir/test_structured_schema_equality.py` passed **122 cases in 0.58 s**. Fresh no-suppression coverage passed **18 cases in 5.39 s** after final test edits. The source author also executed the unchanged-source import-layer/no-suppression/frozen-publication subset: **32 passed in 6.35 s**. Whole-tree Ruff with `--ignore-noqa`, formatting over 847 files, and strict Pyright on the four source/test files passed. Local environment: macOS 27.2 arm64, Python 3.11.17, uv 0.11.7 and existing no-sync dependencies; runtime used an explicit isolated source path. These durations are diagnostics, not support guarantees. The implementing PR records actual independent source-review conclusions and complete final-head CI; neither is inferred from design review.

| Boundary | Required evidence | Current status |
|---|---|---|
| Real admission and borrowed cursors | Admitted native/native and native/arena values, no mutation, exact visited-kind/callback refusal, unchanged model/arena identity | Passed by the focused tests above; original input identity and limitations remain in the implementing PR evidence |
| Traversal and scalar decisions | Length/count/key/value phase order, first mismatch, all scalar kinds, Unicode order, signed zero, boolean separation and unchanged exact-decimal decisions | Passed by the focused tests above; original input identity and limitations remain in the implementing PR evidence |
| Work and auxiliary accounting | Hand-derived generic/fixed-header totals, exact collection/sort/lookup charges, exact/one-short reservations, repeated/shared-meter charging, laziness and no refunds | Passed by the focused tests above; original input identity and limitations remain in the implementing PR evidence |
| Finite owned state and disposal | Depth-16 native/deeper arena comparison, at most 17 live frames, dense-array immediate mismatch, success/false/refusal and real unexpected-error release, explicit caller-traceback disposal | Passed by the focused tests above; original input identity and limitations remain in the implementing PR evidence |
| Independent actual-source review | Separate correctness, security and performance profiles for the combined equality/index boundary and relevant evidence assertions | Actual independent source results and inspected revisions are recorded in the implementing PR and required before merge; design review alone is insufficient |
| Strict and supported-platform checks | Focused changed tests; required lint, format, type, architecture/no-suppression and frozen-publication checks; one successful complete final-head CI on Linux/macOS with ordinary both-database coverage | Local focused/static boundaries above passed; complete final-head Linux/macOS and ordinary database CI remain required before merge. No additional database fixture is needed for pure helpers |
| Complete evaluator and real consumers | Handler/adapter/composition implementation and existing activation/aggregate-admission evidence | Pending implementation outside #519 |

The approved plan records the available daily master mutation audit, run `41025696626` at source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`, as partial pricing/CEL scope with no applicable equality finding. It supplies no equality mutation confirmation and adds no PR mutation campaign or merge hold. Logical owned-state checks remain blocking; no time, heap or RSS observation is recorded as a passing guarantee.

## Tested input identity and integration

Numerical/preparation/model sources and every pre-existing byte of the StructuredOutput specification are unchanged; only the internal-helper subsection is appended. Source SHA-256: cursors `97aa4adc1c79f63f84886407e30f1cdaa122bc2436a4de4e751e362acabaa9bb`, equality `9380ac1d2441df400804a17626bed293cf77eea3c21630bddef27dc6a7094cbf`. Source/test identities and raw JUnit are retained with the implementing PR evidence. The target-only fan-out declaration delivery changes no equality/numerical/arena/model input; its exports, specifications and human wording are preserved by nonoverlapping composition. No new human-document wording is needed for this helper.

## Lookup acceptance completion

Actual-source review identified one missing acceptance case: lookup tests used only equal-length stored keys. The bounded follow-up adds four found/missing and exact/one-short cases with key lengths 6, 7, 37 and 1, placing the longest key inside the canonical order. Expected work derives independently from the fixture maximum, and assertions freeze the stored maximum and paid prefix. The new cases passed in 0.02 s; the original 122 cases and all production source bytes remain unchanged. Their original matched-input evidence is retained separately, for 126 focused cases in total.

Lookup-only Ruff and formatting passed. Four-file strict Pyright passed on the original inspected revision; the new test delta is locally unverified because a later tooling invocation created an incomplete local environment after temporary-link removal. Two local resolution attempts were stopped without changing dependencies or configuration. The complete final-head native strict check remains mandatory and cannot be replaced by this record. The malformed local environment is outside the committed change.
