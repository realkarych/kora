# Standard schema validation

Design under [#1577](https://github.com/realkarych/kora/issues/1577), prepared 2026-11-15 for explicit owner review. This document defines the proposed replacement of the custom metered evaluators under [#1361](https://github.com/realkarych/kora/issues/1361). It implements no validator, stored binding, deployment or consumer. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) governs adoption. Existing policies and their implementation remain unchanged until the replacement is accepted and implemented.

## 1. Scope and immutable identity

One pure implementation in `kora.ir` serves two independently bound domains:

| Domain | Exact identity | Existing saved member |
|---|---|---|
| Effective StructuredOutput schema | `kora.structured-output-schema-policy.v2` | `schema_validation = {policy, parameters}` |
| Graph parameter evaluation | `kora.graph-parameter-evaluation.v2` | `execution_contract.parameter_evaluation = {profile, limits}` |

Both maps save every row of section 2 in its table order, with exactly the stated integer values for that domain; integers are exact native integers, never booleans. No missing key, unknown key, substituted value or reordered map is an executable binding. The map's existing canonical representation participates in the existing contract and deployment digests. There is no second digest or migration/backfill.

The parameter binding remains separate from `parameters = {interpretation, schema}`, whose exact shape, `kora.graph-parameters.v1` interpretation and omission rule do not change. Insert the specified `parameter_evaluation` member immediately after `parameters`; preserve all other relative order. This is a new explicit compatibility binding within `kora.execution.v1`, not permission to reinterpret an old eight-member execution contract.

Standalone StructuredOutput revision 1, canonical bytes, full/selected digests, numeric kinds, signed zero, checker view and ArtifactType ABI stay unchanged. The new execution profile can refuse values admitted by that standalone domain: its node/cardinality and work-support bounds below are additional, visible limits. Derived lists and custom behavior availability retain their independent prerequisites. Schema truth supplies no artifact acceptance, provenance, physical closure, retained projection or current-execution authority.

## 2. Exact capacities

All bounds are inclusive. A value fails the first applicable admission phase in section 3, never becomes schema-false because a bound was reached. Text units are stated explicitly; encoded bytes do not mean Python object bytes.

| Saved key, in order | StructuredOutput | Parameters | Unit and scope |
|---|---:|---:|---|
| `max_value_raw_bytes` | 1048576 | 4194304 | Original typed wire / original manual request envelope; enforce before decoding |
| `max_value_canonical_bytes` | 1048576 | 4194304 | Frozen UTF-8 artifact canonical bytes / original canonical parameter representation |
| `max_value_depth` | 16 | 32 | Artifact container depth with root container 1 / parameter container edges with root 0 |
| `max_value_integer_bits` | 14000 | 64 | Artifact absolute magnitude; parameter signed-64 domain remains separately exact |
| `max_value_nodes` | 262144 | 262144 | Scalar/container/key occurrences in the owned value, including repeated occurrences |
| `max_value_string_bytes` | 1048576 | 1048576 | UTF-8 bytes per string or key; no lone surrogate |
| `max_value_array_items` | 65536 | 65536 | Items in any array |
| `max_value_object_members` | 16384 | 16384 | Members in any object |
| `max_schema_source_bytes` | 1048576 | 1048576 | Sum of compact ASCII canonical complete normalized schema input, including identity/binding metadata |
| `max_schema_nodes` | 16384 | 16384 | All scalar/container/key occurrences in that input, annotations and literal data included |
| `max_schema_depth` | 32 | 32 | Original document container depth, root container 1 |
| `max_schema_documents` | 64 | 64 | Original roots, including inline/parameter root; equal bytes do not merge owners |
| `max_schema_positions` | 2048 | 2048 | Schema-valued positions over the complete selected closure |
| `max_reference_edges` | 4096 | 4096 | Original executable reference occurrences, not distinct targets |
| `max_schema_string_bytes` | 65536 | 65536 | UTF-8 bytes per string/key |
| `max_schema_array_items` | 4096 | 4096 | Items per schema-input array, including annotation/literal arrays |
| `max_schema_object_members` | 4096 | 4096 | Members per schema-input object |
| `max_schema_integer_bits` | 14000 | 14000 | Absolute magnitude before decimal formatting |
| `max_numeric_intermediate_bits` | 32768 | 32768 | Each arithmetic numerator, denominator, cross-product or remainder intermediate |
| `max_call_depth` | 32 | 32 | Root-inclusive potential validation calls; separate from JSON depth |
| `max_expanded_calls` | 131072 | 131072 | Sum of section 4 call certificates across all applicable constraints |
| `max_validation_score` | 67108864 | 67108864 | Section 4 conservative admission units, including both preparations |
| `max_unique_items` | 128 | 128 | Largest value array when a reachable `uniqueItems: true` occurs |
| `max_repr_characters` | 8388608 | 8388608 | Section 5 representation bound for each complete value/schema view |
| `max_variable_bytes` | 268435456 | 268435456 | Admitted simultaneous variable bytes across source, numerical views, representations and temporary text |
| `max_auxiliary_entries` | 1048576 | 1048576 | Finite owned node/table/list/frame/iterator/error entries, section 5 |

These are deliberately conservative product-support choices, not measured performance thresholds. The [derivation record](../research/standard-schema-validation-2026-11-15.md) distinguishes inherited domain limits, proposed support choices and calculated bounds. Passing individual maxima does not guarantee that their combination passes the score or aggregate reservation. In particular, uniqueness, large literal equality and repeated references may refuse well below a shape ceiling.

The existing 16,777,216-unit v1 evaluation meter, frame depth 64 and private handler charges are not claimed preserved by these values. V2 replaces them by the specified admission certificate and depth 32. V1 never acquires these rules. No time, process-memory or native crash margin is inferred. Pin changes or acceptance-affecting admission/adapter changes require a new identity or explicit immutable compatibility decision.

## 3. Preparation, original roots and refusal

1. Bind the exact installed policy/profile and confirm the consumer's source authority and finite slot/byte admission. Missing, unknown or malformed binding refuses before value evaluation. Reserve all input bounds before copying; native metadata acquisition and detached decoding remain with their existing owner.
2. Admit the original value envelope, exact builtin native types and scalar domain before copying or formatting. Reject cycles, lone surrogates, nonfinite numbers and callbacks/subclasses. Keep existing duplicate-name and canonical-domain rules. Iterate bounded structures; count cardinality and remaining bytes/nodes before materializing a complete container. Preserve original identity separately.
3. Admit the complete normalized schema source, declared/parent/inline roles, behavior identities and original closed reference roots. Shape validation and schema keyword argument checks precede evaluation; never submit raw attacker-controlled schemas to a metaschema error collector. Reuse bounded structural checking only after its complete inputs are admitted.
4. Visit every relevant schema-valued position, including reachable definitions and schema positions in selected closure documents. Refuse `pattern`, `patternProperties`, `unevaluatedItems`, `unevaluatedProperties`, unsupported dialects and cycles in potential executable calls. Keyword-looking members inside `const`/enum data are data. Complete structural IR remains representable; executable publication refuses unsupported selected contracts.
5. Resolve `$ref` and pointer-based `$dynamicRef` to exact original roots/positions using the saved closure, including potential references in unchosen branches. No network, filesystem, mutable alias or automatic-code lookup occurs. Kora's existing closed JSON-pointer grammar gives no anchor-name dynamic rebinding. `$recursiveRef`, format, content and unknown annotation keywords have no validation effect under draft 2020-12; they do not initiate retrieval.
6. Calculate all section 4/5 certificates before building the numerical execution views or invoking the library. Addition/multiplication saturates at the relevant limit plus one; refuse before an overflowing/proportional allocation. Memoize by original schema position once per call, retaining edge multiplicity. Compute schema support independently of the particular successful/failed branch.
7. Build one admitted private numerical value view and one closed schema view per source occurrence. In schema positions only, remove `$schema`, `$id`, `$anchor` and `$dynamicAnchor` from the execution view after validating the original meaning. Assign each admitted original schema position a distinct ordinal from 0 through 2047. Normalize both `$ref` and pointer-only `$dynamicRef` to the target position’s fragmentless private resource ID, preserving both keyword names and their independent assertions when they coexist. Register the numerical subtree at each position under `urn:kora:schema:<ordinal>` with explicit draft-2020-12 specification; registrations share owned views, never copy subtrees. Keep equal source roots distinct. Original pointer resolution is already complete before this transformation; the closed registry never sees authored IDs, names or fragments, and its retrieval callback always refuses. Never change saved text, schema literals, diagnostics paths or checker input this way.
8. Evaluate applicable parent-to-declared-to-inline constraints in saved order on the same private value. Reference-only documents are not extra top-level assertions. Close the whole operation before returning a public result; dispose of failed or cancelled results before releasing the caller's reservation.

The numerical view leaves exact native integers unchanged and converts finite floats by their frozen canonical decimal spelling to `fractions.Fraction`, never by their binary float constructor. Convert numerical schema literals, including recursive const/enum data, identically. An integral Fraction satisfies the library's `integer` type; bool does not. The supported number type is exact int or the owned Fraction view. Arithmetic comparisons/divisibility/equality remain mathematical; identity keeps original kind and signed zero. Schema length/cardinality operands are admitted as the nonnegative mathematical integers their keyword requires, and must not cause implicit float conversions.

Removing dialect markers from the private schema positions prevents `evolve` from selecting the unadapted stock class. Tests must cover root and referenced `$schema`, inline roots and nested literal objects containing the same names. A library/adapter invariant failure is a fixed safe internal failure, not validation-false or another policy default.

## 4. Finite admission certificate

The certificate bounds call multiplicity and admits finite structural work; its unit score is not an instruction counter or latency prediction. No meter is inserted into the library's keyword evaluation. All counts below are obtained by bounded source/value traversals before evaluation.

For any JSON subtree, `n` counts containers, scalars and object keys; `b` is its admitted encoded byte width; `k` is its maximum UTF-8 object-key width. Keep maxima for the whole value, every array-element subtree and every const/enum literal subtree, without copying them. Let `A` and `M` be maximum array/member counts in the value; `S` be total schema-input nodes; `K` be maximum key width in schema or value. For a numeric group let `B` bound its largest integer/numerator/denominator bit width; use `L = ceil((2 * B + 2) / 30)` and `P = 16 * L * L`. For each float, derive B before Fraction construction from its bounded canonical decimal spelling: `4 * (significand_digit_count + abs(decimal_exponent)) + 4` is a conservative bound. Native integers use their actual absolute bit length. Require `2 * B + 2 <= max_numeric_intermediate_bits`. Conversion charges use the individual number's B; comparison charges use the maximum B of their possible operands, not the fixed capacity itself.

A conservative recursive-equality score for two subtree bounds is:

`Q(x, y) = (n_x + n_y + 1)^2 * (k_x + k_y + 1) + b_x + b_y + P`.

This deliberately includes worst-case object-key comparison/lookup work, full string walks and bounded rational arithmetic; it can refuse even comparisons that would terminate early. For const/enum use the whole-value and maximum literal bounds. For uniqueness use the maximum value-array-element bounds on both sides. A subtree with no numeric values uses zero in place of `P`; otherwise keep `P`. Empty arrays still have zero element-comparison contribution.

Build the acyclic potential-call multigraph. Per occurrence of an outgoing edge, weight `w` is 1 for each reference keyword, not, all/any/oneOf alternatives, if/then/else, properties, dependentSchemas and prefixItems; `A` for items and contains; `M` for additionalProperties and propertyNames. Definitions/annotations add no call edge unless referenced. Include both conditional outcomes as a conservative choice; no successful branch removes its budget. These weights overestimate missing keys, unchosen branches and prefix items already consumed by items.

In reverse topological order calculate `E(s) = 1 + sum(w * E(target))` and longest root-inclusive call depth. Sum E over every actual constraint root, including repeated execution through separate roles. A shared target is calculated once but contributes on every incoming edge. Cap depth and E before expanding any execution tree; no execution-tree materialization is needed.

Each schema position has a local score, starting with `1 + its keyword count`, plus:

- `P` for each numeric or numeric-length/cardinality assertion that compares numbers;
- `(M + schema_input_member_count + 1)^2 * (K + 1)` for each object-membership family: properties, required, dependentRequired, dependentSchemas, additionalProperties or propertyNames;
- `A + 1` for each array applicator; sequence length plus one for boolean composition;
- `Q(value, literal)` for const, and enum length times that Q for enum;
- `(A + 1)^2 * Q(element, element) + A + 1` for reachable uniqueItems true, in addition to its item ceiling. This covers the exact pairwise uniqueness adapter; no sorting or new hash identity is introduced.

Type checks and direct native length reads add their finite argument count plus one; they do not traverse value content. Numeric assertions whose operands are statically nonnumeric still receive the conservative numeric charge. Add child contributions with the same w recurrence: `W(s) = local(s) + sum(w * W(target))`. Preparation also charges `(S + reference_edges + schema_positions + 1) * (maximum_schema_object_members + 1) * (maximum_schema_key_bytes + 1)`, using observed maxima. Admit this charge before schema lookup/resolution/sorting, after the initial bounded shape scan. Sum W over all constraint roots, that preparation charge, admitted original/source byte widths and node counts, and the individual P for every float-to-Fraction conversion in both views. This total must not exceed max_validation_score. Use one total for the operation, not a fresh allowance per schema, branch or reference.

A true/false boolean schema has local score 1 and no edges. `uniqueItems: false` has no comparison charge. Bounds and exact arithmetic checks occur outside `not`, `if`, anyOf and oneOf, so capacity refusal cannot be converted to successful negation. Execution may perform less work; it cannot choose a larger admitted domain by taking an early success.

## 5. Library adaptation and actual release

Use the pinned draft-2020-12 validator with the exact numeric type checker and exactly three replaced handlers: anyOf, oneOf and uniqueItems. The two composition handlers use the same bounded boolean-decision helper for child schemas; that helper obtains at most one error from iter_errors and explicitly closes its generator on every exit.

AnyOf visits alternatives in saved order and succeeds at the first true child. OneOf visits in saved order, succeeds exactly when one child is true and may stop at the second true child. When their Boolean result is false, yield one fixed internal ValidationError without context, cause or authored message. Do not accumulate child errors. For uniqueItems true, compare every unordered pair with the pinned library’s recursive JSON equality on the numerical view; stop at the first equal pair and yield one fixed context-free mismatch. Keep booleans distinct from numbers at every nesting level. Do not use stock sorting, hashes, a second identity representation or a retained seen-list. An empty/singleton array is unique; false or a non-array has no uniqueness effect. All other supported keyword semantics stay with the pinned library. These are small ownership and exact-equality adaptations, not another metered evaluator or a monkeypatch of library globals.

With the two composition handlers, no reachable keyword constructs a parent/context error tree. Check this premise against the pinned implementation and lock it in regressions: nested anyOf/oneOf inside not/if/contains, an early failed alternative followed by success, and failure/refusal exits. Stock errors may temporarily contain value/schema references and text, but none crosses the pure boundary or enters logging. No error can retain another error through parent/context/cause; close iterators and drop temporary errors before continuing or returning. No forced process-wide collection, GC configuration change, library-global patch or background cleaner supplies release. If the pinned library violates this ownership premise, refuse activation and amend the design rather than claim that outer closure is enough.

Let R be a conservative repr-character bound: strings/keys charge `12 * codepoint_count + 2`; native integers charge a sign plus `ceil(bit_length * 30103 / 100000) + 1`; a Fraction charges both such integer bounds plus 16; fixed scalars charge 5; arrays/objects charge 2 plus child bounds and 4 per child/member. Sum these bounds for each complete numerical value/schema view and enforce max_repr_characters. This includes schema literals and normalized reference strings. No authored override can provide repr.

Reserve at least `8 * (R_value + R_schemas + 256)` variable bytes for temporary representations and one message (two simultaneous copies, up to four bytes per character), plus all simultaneously live original canonical/source bytes, owned string/numeric bytes in both numerical views, registry keys and any copied/sorted key arrays. Integer/Fraction storage charges each admitted numerator/denominator at `ceil(bits/8)` plus a fixed 32 bytes; charge both operands and bounded arithmetic intermediates before conversion. Fail before construction if the sum exceeds max_variable_bytes. This variable-byte admission is deliberately separate from Python object overhead.

An auxiliary entry is a logical ownership unit, not a byte or Python allocation. Count one per JSON container/scalar/key occurrence in each simultaneously owned tree. A repeated occurrence counts again even when it aliases the same object. Outside those trees, count one per fixed-shape record and one per occupied element/row of a variable list, tuple, set, deque or map. A fixed-shape record includes its statically bounded fields and fixed tuples/dictionaries of constructor arguments; its fields are not charged again. Variable containers referenced by such a record are charged separately, including repeated alias slots. Container headers, allocator overhead and internal hash/trie implementation nodes are not entries; the enclosing finite cardinality remains their bound. Strings/numerators/denominators charge variable bytes separately. These definitions cannot be used to classify an authored-sized container as a fixed record.

Use observed dimensions. Let C be admitted call depth, Vd the largest value container nesting with root container 1, Sd the largest schema container nesting with root container 1, SA the largest schema array and SM the largest schema object. Reserve the sum of this simultaneous ledger before execution:

| Ledger term | Covered simultaneous ownership |
|---|---|
| `2 * value_nodes` | Original owned value and numerical view, counting full occurrence trees even where immutable leaves are shared |
| `4 * S` | Original/normalized schema source, numerical schema tree and at most one additional full-tree preparation/checking representation; original and normalized may coincide but retain both charges |
| `12 * schema_positions` | Per-position source/view association, support statistics and graph traversal/result records, ordinal mapping, registry row and Resource; twelve fixed records/rows per position are reserved, including all indexes |
| `8 * reference_edges + 4 * schema_documents` | Original/normalized edge and resolved-target records plus document/role/root indexes; no tree copies in these rows |
| `256 * (C + Vd + Sd + 1)` | Fixed validator, resolver, Resource, evolve-argument, error, iterator and traversal/decision-frame records; each level reserves 256 such records independently of variable containers. Numeric/equality and preparation recursion are charged even when schema-call depth is 1 |
| `4 * C * C` | Variable resolver-history slots and error path/schema-path slots across simultaneously active calls, including aliased persistent-list tails |
| `C * (2 * A + 4 * M + 2 * SA + 4 * SM)` | Variable scratch slots: array slices/index buffers; additionalProperties extras set, sorted list and representation iteration; schema alternative buffers; active keyword triples/list slots and schema-member scratch indexes |

A keyword triple is one fixed record plus its one slot in the validator’s variable list, so it uses two of the four SM allowances. The fixed kwargs/attribute fields created by validator/attrs evolve consume fixed-record allowance; an error’s bounded constructor tuple/attributes do too, while nonempty path/schema-path deques consume the separate quadratic path allowance. No context children exist. Each resolver history has at most C prior URI slots and there are at most C simultaneously active resolvers. Conservatively allow one error per active call, with path length at most C and schema-path length at most `2 * C`; this adds at most `3 * C * C` slots. The combined `4 * C * C` allocation counts shared history tails again through each live owner, without depending on physical sharing. Removing dynamic anchors does not remove the library’s history. AdditionalProperties may hold both its extras set and sorted list at once; the four M allowances also cover temporary iteration slots while building their bounded representations. Pairwise uniqueness keeps no seen/sorted collection. The fixed-record allowance covers the inspected call families, not an inferred bound on all interpreter objects.

The representation must fit each ledger allocation and the sum must not exceed max_auxiliary_entries before execution. Capacity from one row cannot hide an unaccounted structure in another. An implementation cannot add another full tree, authored-sized record field, registry copy or temporary collection without reconciling this ledger and the immutable policy. The implementing PR maps its exact owned structures and pinned-library active call path to these allocations; if that cannot be established, activation refuses and the design must change. Returned accepted identity/model and the prepared-contract holder have named separate owners. Do not retain complete failed views in errors, caches, thread locals or reusable registries.

The installation owns a fixed pool of the 2048 ordinal resource-ID strings, each at most 20 ASCII bytes, separately from operation admission. Standard URL parsing may cache only these content-free identifiers and fixed empty/dialect strings; no authored pointer, key, value, schema or registry may enter that shared cache. Its code-only keys/results remain library/runtime-owned, not operation-owned. Supported interpreter/library versions must retain a finite parser cache; implementation records the actual cache capacity and verifies the absence of operation content rather than flushing global state. The design guarantees finite admitted structures and no live operation-owned reference after settlement, not zero allocator retention or process RSS. Caller concurrency, aggregate reservations and scheduling fairness remain separate activation obligations. Cancellation/expiry cannot preempt a synchronous library call: discard its result and retain the slot until iterator/state release completes. Unexpected internal failure reaches the existing safe failure boundary; discard original exception/traceback ownership before declaring settlement, and retain the slot until that cleanup actually finishes. A raised exception alone does not establish release. Shutdown stops admission and drains actual active work. No database unit spans validation.

## 6. Composition and compatibility

The protocols composition receives an installation-supplied exact map from immutable validation/digest/summary identities to trusted behavior instances. Neither ArtifactType nor ProviderMetadata currently supplies that map by itself. Exact builtin mappings come from verified frozen catalogue bindings; unknown custom identities refuse. Generic `kora.json-schema-2020-12.v1` validation uses this exact common decision without a prior stock-numeric rejection. All non-schema trusted behavior and inherited model admission remain required. Schema data cannot load code or select a provider by a claimed name.

Only complete valid composition yields an owned value for effective digest/summary extraction. Invalid carries the existing fixed root SO006 where applicable; unsupported/capacity dispositions retain the existing fixed refusal vocabulary. Preserve exact model admission SO001–SO005. Error text and schema paths are not copied into audit or operational records. Trusted installed behavior is cooperative code, not sandboxed by this policy.

| Saved state | Authorized history / matching receipt replay / closure | New validation effects |
|---|---|---|
| Exact old output v1 or reserved parameter v1 | Preserve original fields, bytes, digests and saved outcomes; existing authorization/order applies | Unavailable; never reinterpret as v2 |
| Old whole execution contract without parameter_evaluation | Preserve old shape and original receipt outcome | Cannot acquire the new parameter interpretation; require a new complete graph version |
| Exact implemented v2 binding | Preserve its exact immutable identity | Only complete supported producer/reader/composition may execute it |
| Missing/unknown/malformed binding | Existing opaque historical access and closure rules; no repair | Existing unavailable refusal; no current-default fallback |

Checking readers dispatch from the admitted saved binding; new constructors select a trusted installed binding explicitly. Apply this distinction throughout graph_outputs, graph_schema_preparation, graph_output_inventory, graph_cel_facts and the whole graph_execution_contract reader. Do not rebuild historical expected data using today's policy constant. Preserve the existing independent execution/deployment/IR digest scopes and old member ordering. API authorization and request envelope precede permanent accepted/skipped receipt replay; changed request identity remains API004; absent executable parameter interpretation remains API010. No owner-approved replay outcome is replaced by validation under a later policy.

Executable publication under v2 verifies support, immutable behavior map and complete normalized schema certificates. Value-dependent admission occurs before each evaluation; publication does not promise every possible domain value fits. Until whole v2 construction/reading is implemented, no current default emits the proposed bindings.

## 7. Verification and delivery

The design PR has no production code, dependency or CI change. Implementation remains #1361: shared validator, effective StructuredOutput composition and distinct parameter composition, followed by removal of retired evaluators under the normal 1,000-line ceiling. The [planning record](https://github.com/realkarych/kora/issues/1577#issuecomment-6886921639) gives component estimates; exact implementation scope is reconciled against this design before edits.

Named blocking implementation cases:

- `test_standard_schema_numeric_view_preserves_decimal_semantics`: const/enum/uniqueItems/order/multipleOf, bool versus number including uniqueItems on `[[1], [true], [1]]` and `[[false], [0], [false]]`, integral floats, signed zero and extrema; original bytes unchanged.
- `test_standard_schema_adapter_survives_dialect_and_reference`: root/referenced dialect markers, original local/URN roots, coexisting `$ref`/`$dynamicRef` with contradictory targets, distinct equal roots and marker-shaped literal data.
- `test_standard_schema_preflight_bounds_expansion`: repeated-reference diamonds and long shallow-JSON call chains refuse before library execution, including false/negated branches, chains/diamonds through not and a not/reference cycle.
- `test_standard_schema_capacity_boundaries`: each exact table limit and the combined score, distinct depth origins, strings, integers, copies and saturated arithmetic; exact logical-entry units, each simultaneous ledger allocation and the total, including a chain of distinct ordinal references with simultaneously live resolver histories; no default coercion.
- `test_standard_schema_errors_release_owned_values`: nested failure, failed-then-success alternatives, false/valid/refused outcomes, iterator closure and absence of retained error graphs without forced GC; after reference resolution, only the fixed ordinal identifiers may remain in shared URL caches, with no authored pointer or schema/value ownership.
- `test_standard_schema_diagnostics_exclude_content`: values, keys, schema text, exception chains and DEBUG; fixed public dispositions only.
- `test_effective_schema_composition_preserves_every_contract`: parent/declared/inline roles, unavailable behavior, inherited refusal and no premature extraction.
- `test_schema_policy_history_and_replay_are_immutable` and `test_parameter_profile_round_trip_and_handoff`: actual full writer/reader chain, old/new shapes, changed hashes and exact binding checks on SQLite and PostgreSQL, authorization/replay ordering and detached validation.

Existing caller cancellation, expiry, shutdown and actual resource settlement remain blocking when those callers activate. Timing/RSS diagnostics do not replace these cases or create new thresholds. Preserve supported-platform CI, strict static checks and one complete successful final-head CI per implementing PR. A schema-only test is not evidence for accepted artifacts, storage cleanup, runtime provenance or physical settlement #1472.
