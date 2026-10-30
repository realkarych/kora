# StructuredOutput effective-schema design provenance and provisional capacities

Recorded on 2026-10-30 against master `4b5fd58937a7d1ed4a89a96cdf8d388d027aeb57`. [Issue #493](https://github.com/realkarych/kora/issues/493), coordinated under [#138](https://github.com/realkarych/kora/issues/138), publishes the documentation-only design. The project owner [approved the design plan and exact human-document wording](https://github.com/realkarych/kora/issues/493#issuecomment-6527475784). The [published plan](https://github.com/realkarych/kora/issues/493#issuecomment-6527347384) and [independent design results](https://github.com/realkarych/kora/issues/493#issuecomment-6527347915) record its scope and review provenance. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3](../specs/artifacts/StructuredOutput.md#3-saved-effective-schemas-and-cel-view) own the accepted architecture and activation prerequisites; this research record creates no runtime policy.

The accepted direction is common pure protocols composition over trusted bound type behavior and an IR-owned restricted in-process evaluator. Regex keywords, unevaluated-item/property keywords and cyclic potential validation calls are excluded from the initial profile. Standalone model/provider evidence remains separate from complete effective validation and actual consumers. No production code, dependency, provider ABI, frozen artifact publication, storage format or import permission changes in the design delivery.

## UNAPPROVED capacity envelope

All thirteen values below are provisional sizing choices from the plan, expressly outside owner approval. They are neither runtime limits, defaults, measurements nor a complete executable `kora.structured-output-schema-policy.v1`. That identity remains reserved/unavailable. A concrete owner-approved specification must first define normalized accounting representations, these numerical parameters and a complete deterministic charging schedule; numerical ceilings alone are insufficient. The first affected implementation plan supplies that specification before code or executable publication.

| Proposed saved parameter | Unapproved value |
|---|---:|
| `max_input_bytes` | 1,048,576 canonical UTF-8 bytes |
| `max_input_nodes` | 16,384 values plus object-member keys, counted separately |
| `max_input_depth` | 32 containers |
| `max_schema_documents` | 64 original documents |
| `max_schema_positions` | 2,048 schema positions |
| `max_reference_edges` | 4,096 |
| `max_numeric_bits` | 14,000 per integer scalar anywhere in retained input |
| `max_numeric_intermediate_bits` | 16,384 |
| `max_preparation_work` | 16,777,216 units |
| `max_evaluation_work` | 16,777,216 units |
| `max_evaluation_depth` | 64 active schema frames |
| `max_auxiliary_bytes` | 4,194,304 owned encoded/temporary buffer bytes |
| `max_auxiliary_entries` | 32,768 fixed-shape auxiliary records |

If later selected, bounds would be inclusive and operation-wide. Input accounting would cover the whole relevant original-document closure, inline/checker schemas, source/target/binding data, behavior descriptors and policy map together, without resets per parent/document/branch. Root container depth would be one. Repeated native-object occurrences would count separately. The normalized representation and occurrence rules are not yet concrete enough for implementation. Existing generic IR admission/export, frozen payload capacities and undecided derived-list aggregate limits remain unchanged. No payload item limit is proposed; an unconstrained admitted dense array must avoid eager per-item auxiliary state.

Auxiliary bytes are reserved sums of encoded/raw/temporary buffers before allocation, not Python heap/RSS. Fixed-shape auxiliary records would have bounded fields and integer IDs/offsets, with variable bytes charged separately. Owned admitted snapshots remain separately bounded; additional execution views/caches count as auxiliary state, and copied trees cannot hide behind record counts. The proposed intermediate-bit envelope is intended to cover products for admitted integers and finite binary64 canonical decimals, subject to pre-operation checks and work admission; it is not measured evidence.

The plan's charging sketch names visits, bytes, comparisons, lookup/collision/sort work and integer-limb arithmetic. Exact formulas/algorithms for byte/key comparison, lookup, sorting, powers, conversion, normalization and handler/frame/dispatch work remain prerequisites. No partial formula is an executable schedule. Resource/closure/no-retainer assertions will be blocking when implemented; CPU/RSS/traced-heap observations remain diagnostics without new thresholds. Per-output bounds establish no aggregate process or 1,000-node graph envelope.

## Source limits

The design preparation inspected the official [validator extension/evolution documentation](https://python-jsonschema.readthedocs.io/en/stable/creating/) and [reference-resolution documentation](https://python-jsonschema.readthedocs.io/en/stable/referencing/), together with pinned `jsonschema` 4.26.0 keyword/utils/validator and `referencing` 0.37.0 source, on 2026-10-30. These sources explain engine extension and reference mechanics; they supply no bounded-work or runtime-conformance guarantee. The last inspected partial master mutation audit covered pricing/CEL, not this evaluator; it establishes no component result or new mutation campaign. Preparation, arithmetic, handlers, composition and real consumer evidence remain pending their independently planned deliveries.
