# Graph parameter omission: decision and consumer activation

Status: public-contract choice accepted by the project owner on 2026-11-03 under [issue #642](https://github.com/realkarych/kora/issues/642); approval-checker consumption is implemented under [#644](https://github.com/realkarych/kora/issues/644); other consumers remain pending. The [proposal](https://github.com/realkarych/kora/issues/552#issuecomment-6601656904) and [independent design review](https://github.com/realkarych/kora/issues/552#issuecomment-6601669353) record the alternatives and review evidence. The normative rules are in [IR](../specs/ir.md#graph-parameter-schema-omission), [API](../specs/api-and-agent-surface.md#admission-state-and-bounded-starts-adr-0020) and [DSL](../specs/dsl.md#1-surface).

## Decision and consequences

An omitted `graph.params` means the effective schema `{"type":"object","properties":{},"additionalProperties":false}`. A new start accepts only an empty parameter object. An explicitly authored empty JSON Schema remains permissive, with the existing plain CEL dynamic view and the API's unchanged JSON-object envelope. The alternative of making omission permissive was not selected: parameters must be explicitly declared or explicitly opened by the author.

The document keeps its authored omission and original identity; the effective schema is not inserted into canonical export. There is no equivalence or deployment deduplication between omitted and explicitly authored schemas. The decision introduces no parameter-value defaulting, coercion, removal or insertion, and settles no other authored-schema default policy.

The empty record uses the existing mapper and checker. Unknown dot/literal-key reads receive the existing `IR011`; dynamic-key indexing can still type-check and fail at evaluation with `missing_field`. Root and stored bodies share the graph-level context. No input/output context, schema-truth rule, projection bound or CEL binding rule changes.

Rerun overlays the original parameters key by key. Against a selected version with omitted schema, inherited nonempty parameters fail `API003`, including when rerun supplies an empty override. Callers can request a fresh empty start through the ordinary trigger operation. No clearing operation or bypass of ordinary authority, recovery, target or quota checks is created.

[ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md) keeps current authority and envelope checks ahead of durable receipt lookup. Matching accepted/skipped replay returns the original outcome before today's schema validation; changed same-key requests remain `API004`. Original semantic request hashing and existing receipt formats are unchanged.

[ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) and [data-model section 11](../specs/data-model.md#11-immutable-contracts-and-execution-records-adr-0018) remain the immutable-interpretation gate. Future activation must identify supported saved semantics before new effects; it cannot silently reinterpret existing `kora.execution.v1` contracts or reconstruct missing meaning from current defaults. This decision chooses no new record field, codec, semantics identity or compatibility mapping.

## Implementation and verification ownership

The structural model preserves missing fields. Approval checking under #640/#644 uses the effective omitted schema but remains partial because input contexts are unavailable. Target-parameter checking under #554 still leaves omitted parameter context unresolved. The original #642 contract publication changed no runtime behavior; #644 adds only the pure approval-checker consumer described below. #552 coordinates effective parameter contexts and full approval validation; #593 owns deployment and canonical-start integration. Compiler work item 3 owns emission of the explicit closed-empty schema for a graph function with no parameters.

The following are pending consumer acceptance obligations, not passing tests or newly implemented checks:

| Owner | Required regression at activation |
|---|---|
| IR context producer, #552 / work item 3 | The approval-only leg is implemented under #644 below. Full input-context composition, target and other parameter consumers still need the same effective meaning, unchanged authored export/identity and independent completeness checks. |
| Compiler, work item 3 | A graph function without parameters emits the exact explicit closed-empty schema; ordinary annotated parameters retain their existing contract and deterministic output. |
| API/engine, #593 | New starts accept empty params and refuse nonempty params with API003 before effects; explicit permissive schemas retain their meaning, with ordinary envelope/data/admission checks. Exercise PostgreSQL and SQLite. |
| Rerun, #593 | A nonempty original run rerun against a latest/named omitted-schema version is refused, even with an empty override; the original run remains unchanged and a fresh empty trigger follows normal admission. Exercise both databases. |
| Replay and current authority, #593 | Matching accepted/skipped receipts remain stable after deployment; changed same-key requests remain API004; current authorization and envelope rules precede replay. Original request hashing is preserved on both databases. |
| Saved graph interpretation, #593 | Supported immutable semantics govern validation and execution; unavailable or missing saved interpretation refuses new effects without altering historical outcomes. Any new persisted format or compatibility decision needs its own reviewed plan. |

The normative publication adds no public operation, CLI/MCP mapping, database mutation, executable component or diagnostic event. The #644 extension retains the existing pure checker and diagnostic event. Their implementation changes retain parity and observability obligations. Existing threat assumptions cover authored input, disclosure, immutable history and honest completeness (TM-04/06/07/10/12 plus authorization/replay TM-01/02/11); no new actor, authority or containment guarantee requires a threat-model amendment. No resource algorithm or numerical method is changed, so this publication claims no performance result.

## Approval-checker consumer under #644

`validate_approval_response_predicates` now removes only omission-based parameter deferral. Its existing lazy mapping creates the exact fixed closed-empty schema for `MISSING`, maps it through the plain `SchemaTypes` and caches the complete `Variable` in the existing invocation. Authored schemas, including `{}`, retain their existing behavior; no global mutable schema or new public helper is introduced. Input identifiers/binders anywhere still defer the whole expression, including dead branches, and parser/schema failures keep their owners.

Parameter-only unknown reads and shadowing are complete typed refusals, with existing fixed `IR011` messages. Dynamic-key indexing can pass static checking without proving that evaluation will find a key. The result/event schemas are unchanged, and the result retains no document, schema, type, bound or syntax tree. Mapping and checker algorithms, per-predicate traversal and lifetime methods are unchanged; the fixed three-key schema supplies no new scale dimension or latency/RSS guarantee.

| Boundary | Executable coverage |
|---|---|
| Omitted versus authored empty/closed-empty, root/stored scope and unchanged export | `test_omitted_approval_parameters_use_closed_empty_schema` |
| Retained old context cases and mixed/dead input dependencies | `test_explicit_approval_classifies_required_context`, `test_omitted_approval_parameters_do_not_complete_input_context` |
| Lazy mapping once per invocation | `test_omitted_approval_parameter_mapping_is_lazy_and_once` |
| Exact release on success, typed refusal, response-schema failure after parameter mapping and input deferral | `test_approval_parameter_context_releases_bound_state`; existing owned-state regressions remain active |
| Scalar diagnostics, private-key/literal exclusion, stdout and real blocked/broken sink shutdown | `test_explicit_approval_logging_excludes_content`, `test_explicit_approval_survives_unavailable_sink`, including omitted mode |
| Installed core without evaluator import | `test_type_schema_validation_works_from_installed_core_only_wheel`, including omitted-schema cases |

The [implementation issue](https://github.com/realkarych/kora/issues/644) and its PR retain actual independent correctness/security results, commands and final-revision CI evidence. TM-04/06/07/10/12 cover the changed interpretation, content exclusion, existing resource bounds and honest completeness; no new actor, authority or containment guarantee changes the threat model. This authored-document operation does not check saved execution-semantics compatibility or authorize public validation, start/rerun or approval execution. Those obligations remain with the owners above.

## Evidence limits

The normative change is checked through independent correctness/security review, changed-document Markdown and offline-link validation, and final-head CI. These checks establish consistency and repository health, not enforcement of pending consumers. The full validator, real provider facts and graph execution remain separate capabilities under #552/#549/#593.

The latest inspected master mutation audit, [41622297131](https://github.com/realkarych/kora/actions/runs/41622297131), used source/tooling `5b8f0981cbf5b23fe184eca3bbc36b8c68d55d87` and reported `execution_failed` with zero executed or reused mutations. The retained successful [41297342266](https://github.com/realkarych/kora/actions/runs/41297342266) at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` was a partial pricing/CEL-path audit, with no parameter-omission finding. Neither supplies omission enforcement evidence or adds a PR mutation campaign.
