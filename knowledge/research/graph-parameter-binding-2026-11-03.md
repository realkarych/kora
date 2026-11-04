# Saved graph parameter interpretation

Status: logical binding and compatibility choice accepted by the project owner on 2026-11-03 under [issue #656](https://github.com/realkarych/kora/issues/656), a child of [#593](https://github.com/realkarych/kora/issues/593). The [exact plan](https://github.com/realkarych/kora/issues/656#issuecomment-6611583667), [independent design results](https://github.com/realkarych/kora/issues/656#issuecomment-6611597225) and [owner approval](https://github.com/realkarych/kora/issues/656#issuecomment-6612093749) distinguish the decision from implementation. Normative representation and activation requirements are in [data-model section 11.1](../specs/data-model.md#graph-parameter-interpretation); the authored meaning remains in [IR parameter omission](../specs/ir.md#graph-parameter-schema-omission).

## Decision and alternatives

The accepted binding stores `interpretation: kora.graph-parameters.v1` and the exact effective `schema` in the mandatory two-member `execution_contract.parameters` object. It is an explicit immutable compatibility mapping within `kora.execution.v1`, as permitted by [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md). The record format, IR format, existing built-in descriptors, plain CEL mapping and artifact compatibility bindings remain unchanged.

The alternative was a new whole-engine semantics identity. It would require a wider compatibility plan for existing descriptors and consumers without a demonstrated need for that scope. The narrower mapping was selected. Treating an old missing binding as a current default was excluded: an immutable identity cannot supply unrecorded prior meaning.

The effective schema is the already accepted closed-empty object for omission, or the exact authored logical schema, including `{}`, annotations and reference spelling. Original schema roots and the saved type/reference closure retain authority. The existing contract and deployment digests include the binding; there is no separate parameter digest. Complete IR remains part of deployment identity, preserving the distinction between omission and an explicitly authored closed-empty schema. Copying schema annotations applies no value defaults and selects no new authored-schema default policy.

Missing, unknown, malformed or inconsistent bindings make new effects unavailable. Existing history, current-authority checks, matching accepted/skipped replay and closure/recovery obligations remain intact. New execution needs a new complete graph version unless a separately accepted compatibility mapping supplies its original meaning; no old-version rewrite or inferred backfill is authorized.

## Logical schema versus physical persistence

At inspected base `a2871ff6ff07d3b692bdc06280797c8c8d2b2279`, graph-version columns and raw row reads exist, but a complete execution-contract producer/codec and supported-interpretation reader do not. Existing `JsonDocument` admission rejects NUL, while the IR boundary admits it in schema annotations. PostgreSQL JSONB storage also does not by itself establish canonical numerical identity preservation. This design does not assert that current row serialization can store the complete admitted schema domain unchanged.

The future writer must prove exact logical-schema and canonical-identity round trips on SQLite and PostgreSQL, including NUL-bearing annotations, supported numerical values, numeric-kind/signed-zero identity and original reference roots. If the approved saved-value representation cannot preserve that domain, a separate bounded physical-representation decision is required. Silent annotation removal, normalization or narrowing of admitted schema values is not an implementation option authorized here.

No table, column, codec, migration, parameter value, dependency, provider ABI, resource owner or public operation is added by the contract publication. Existing 4-MiB/depth-32 request-identity limits and separate CEL projection/data limits retain their owners. This decision creates no new numerical verification method or gate.

## Implementation owners and pending checks

The complete writer/reader and canonical-start integration belong to #593. Complete trusted provider facts remain under [#549](https://github.com/realkarych/kora/issues/549), and complete approval contexts under [#552](https://github.com/realkarych/kora/issues/552). The complete validator and every other saved-contract prerequisite must be implemented before deployment or start activation. Neither a valid parameter member, a digest nor a caller-provided validation claim is execution authority.

The following are pending producer/reader integration obligations, not tests implemented or passed by this contract change:

| Owner | Named acceptance test at activation | Required behavior |
|---|---|---|
| Complete graph-version writer, #593 | `test_graph_version_preserves_parameter_interpretation` | Omitted versus authored `{}` and authored closed-empty remain distinct; original schema/root/closure and existing contract/deployment identities are deterministic and preserved. |
| Physical writer/reader, #593 | `test_graph_version_parameter_schema_round_trip` | On SQLite and PostgreSQL, preserve the full admitted logical schema and canonical scalar identity, including NUL, supported numeric kinds/signed zero and local/saved references; no silent domain narrowing. |
| Complete interpretation reader, #593 | `test_graph_version_parameter_binding_refuses_incompatible_records` | Missing, unknown, malformed, corrupted or IR-inconsistent bindings refuse before new effects; old versions and outcomes are unchanged and no current defaults or aliases supply missing meaning. |
| Start/replay integration, #593 | `test_manual_start_replay_preserves_parameter_contract_boundary` | Current authorization and envelope checks precede receipt lookup; matching accepted/skipped results survive deployment and unavailable current interpretation; changed requests remain API004. Exercise both databases and denied current access. |

Public error integration, safe diagnostics, failure/cancellation/shutdown behavior and resource closure retain their existing owners. No runtime event or logging sink is introduced by publishing the contract. Current authorization, secret and payload-retention boundaries remain independent of saved interpretation.

## Review and evidence limits

The exact approved plan has SHA-256 `f33c7ec0af4752c5bea943c3d116b54663dec179f223879a5707a583545fbb84`. Independent correctness and security design reviews inspected that plan, the accepted ADR/IR/data-model/API contracts and corresponding current source against `a2871ff6ff07d3b692bdc06280797c8c8d2b2279`. Both passed for owner selection. Their actual identities, inspected inputs, restrictions and conclusions are recorded in the linked review result; neither review selected the decision for the owner or supplied executable evidence.

Correctness clarified that omission and an explicitly authored closed-empty schema do not deduplicate, and exposed the current storage-domain gap. Security verified explicit immutable interpretation, closed reference authority, current authorization/replay order, honest historical outcomes and incomplete-consumer refusal. The final approved proposal includes both-database full-domain persistence as a prerequisite and prohibits silent normalization or narrowing. Existing threat-model controls cover these boundaries; no new actor or containment guarantee requires a threat-model amendment.

The four-file contract change requires applicable Markdown/offline-link checks, independent actual-diff correctness/security review and one complete successful final-head CI. The issue and PR own actual final-revision results. Production added/deleted lines are zero. Documentation checks establish consistency and repository health, not writer/reader enforcement, Linux/macOS runtime qualification or lossless database persistence.

The latest master audit inspected during planning was failed run [41622297131](https://github.com/realkarych/kora/actions/runs/41622297131) at `5b8f0981cbf5b23fe184eca3bbc36b8c68d55d87`. Retained successful [41297342266](https://github.com/realkarych/kora/actions/runs/41297342266) at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` covers pricing/CEL paths only and supplies no applicable saved-contract finding. No mutation campaign or merge hold is introduced. Existing failed CI and native diagnostic outcomes are not reclassified by this decision.

## Physical foundation under #658

On 2026-11-03 the owner accepted [the explicit canonical JSON representation](https://github.com/realkarych/kora/issues/658#issuecomment-6613599009). [Its record](graph-value-storage-2026-11-03.md) describes the nullable physical discriminator, paired canonical text, bounded pure codec and additive non-downgradable migration. Canonical JSON, rather than every original Python code-point spelling, defines the saved logical value. Decoded-name collisions refuse before writes. This clarification changes no domain digest algorithm and never backfills a missing interpretation. The physical codec and SQL boundary tests do not complete the activation table above: the real complete-contract producer, supported-interpretation reader and start integration remain owned by #593.

## Parameter-section implementation under #660

The [section constructor and reader](graph-parameter-construction-2026-11-03.md) implement the accepted logical binding with bounded canonical extraction from pinned IR and exact saved-schema comparison. Real both-database tests exercise physical persistence and verification after namespace-scoped read resources close. This establishes only the parameter section; the complete writer/reader and start activation checks in the table above remain pending. No new binding, public numerical limit, provider authority or compatibility fallback is introduced.
