# Graph parameter omission: decision and pending activation

Status: public-contract choice accepted by the project owner on 2026-11-03 under [issue #642](https://github.com/realkarych/kora/issues/642); consumer implementation remains pending. The [proposal](https://github.com/realkarych/kora/issues/552#issuecomment-6601656904) and [independent design review](https://github.com/realkarych/kora/issues/552#issuecomment-6601669353) record the alternatives and review evidence. The normative rules are in [IR](../specs/ir.md#graph-parameter-schema-omission), [API](../specs/api-and-agent-surface.md#admission-state-and-bounded-starts-adr-0020) and [DSL](../specs/dsl.md#1-surface).

## Decision and consequences

An omitted `graph.params` means the effective schema `{"type":"object","properties":{},"additionalProperties":false}`. A new start accepts only an empty parameter object. An explicitly authored empty JSON Schema remains permissive, with the existing plain CEL dynamic view and the API's unchanged JSON-object envelope. The alternative of making omission permissive was not selected: parameters must be explicitly declared or explicitly opened by the author.

The document keeps its authored omission and original identity; the effective schema is not inserted into canonical export. There is no equivalence or deployment deduplication between omitted and explicitly authored schemas. The decision introduces no parameter-value defaulting, coercion, removal or insertion, and settles no other authored-schema default policy.

The empty record uses the existing mapper and checker. Unknown dot/literal-key reads receive the existing `IR011`; dynamic-key indexing can still type-check and fail at evaluation with `missing_field`. Root and stored bodies share the graph-level context. No input/output context, schema-truth rule, projection bound or CEL binding rule changes.

Rerun overlays the original parameters key by key. Against a selected version with omitted schema, inherited nonempty parameters fail `API003`, including when rerun supplies an empty override. Callers can request a fresh empty start through the ordinary trigger operation. No clearing operation or bypass of ordinary authority, recovery, target or quota checks is created.

[ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md) keeps current authority and envelope checks ahead of durable receipt lookup. Matching accepted/skipped replay returns the original outcome before today's schema validation; changed same-key requests remain `API004`. Original semantic request hashing and existing receipt formats are unchanged.

[ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) and [data-model section 11](../specs/data-model.md#11-immutable-contracts-and-execution-records-adr-0018) remain the immutable-interpretation gate. Future activation must identify supported saved semantics before new effects; it cannot silently reinterpret existing `kora.execution.v1` contracts or reconstruct missing meaning from current defaults. This decision chooses no new record field, codec, semantics identity or compatibility mapping.

## Implementation and verification ownership

The structural model preserves missing fields. Existing declared-parameter approval checking under #640 and target-parameter checking under #554 remain partial; they still report omitted parameter context as unresolved. This contract publication does not change their runtime behavior. #552 coordinates effective parameter contexts and full approval validation; #593 owns deployment and canonical-start integration. Compiler work item 3 owns emission of the explicit closed-empty schema for a graph function with no parameters.

The following are pending consumer acceptance obligations, not passing tests or newly implemented checks:

| Owner | Required regression at activation |
|---|---|
| IR context producer, #552 / work item 3 | Omitted schema maps to the exact closed-empty type; authored `{}` remains dynamic; root/stored contexts, literal/dynamic-key behavior, unchanged export/digest and independent incomplete inputs are covered. Target and other parameter consumers use the same meaning. |
| Compiler, work item 3 | A graph function without parameters emits the exact explicit closed-empty schema; ordinary annotated parameters retain their existing contract and deterministic output. |
| API/engine, #593 | New starts accept empty params and refuse nonempty params with API003 before effects; explicit permissive schemas retain their meaning, with ordinary envelope/data/admission checks. Exercise PostgreSQL and SQLite. |
| Rerun, #593 | A nonempty original run rerun against a latest/named omitted-schema version is refused, even with an empty override; the original run remains unchanged and a fresh empty trigger follows normal admission. Exercise both databases. |
| Replay and current authority, #593 | Matching accepted/skipped receipts remain stable after deployment; changed same-key requests remain API004; current authorization and envelope rules precede replay. Original request hashing is preserved on both databases. |
| Saved graph interpretation, #593 | Supported immutable semantics govern validation and execution; unavailable or missing saved interpretation refuses new effects without altering historical outcomes. Any new persisted format or compatibility decision needs its own reviewed plan. |

No public operation, CLI/MCP mapping, database mutation, executable component or diagnostic event is added here. Their implementation changes retain parity and observability obligations. Existing threat assumptions cover authored input, disclosure, immutable history and honest completeness (TM-04/06/07/10/12 plus authorization/replay TM-01/02/11); no new actor, authority or containment guarantee requires a threat-model amendment. No resource algorithm or numerical method is changed, so this publication claims no performance result.

## Evidence limits

The normative change is checked through independent correctness/security review, changed-document Markdown and offline-link validation, and final-head CI. These checks establish consistency and repository health, not enforcement of pending consumers. The full validator, real provider facts and graph execution remain separate capabilities under #552/#549/#593.

The latest inspected master mutation audit, [41622297131](https://github.com/realkarych/kora/actions/runs/41622297131), used source/tooling `5b8f0981cbf5b23fe184eca3bbc36b8c68d55d87` and reported `execution_failed` with zero executed or reused mutations. The retained successful [41297342266](https://github.com/realkarych/kora/actions/runs/41297342266) at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` was a partial pricing/CEL-path audit, with no parameter-omission finding. Neither supplies omission enforcement evidence or adds a PR mutation campaign.
