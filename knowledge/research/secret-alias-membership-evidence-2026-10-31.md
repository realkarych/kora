# Declared secret-alias membership evidence — 2026-10-31

[Issue #525](https://github.com/realkarych/kora/issues/525) owns this work-item-3 pass. Accepted behavior comes from [IR sections 8 and 10](../specs/ir.md), [protocols section 6](../specs/protocols.md), the [shared diagnostic contract](../specs/diagnostics.md) and ADR-0009/0011/0018. This record describes the static boundary; exact reviewed/tested revisions, independent reviewer provenance and final CI evidence belong to the linked pull request.

## Scope and ownership

`validate_secret_aliases` accepts an unchanged structurally validated `GraphDocument`. The call checks exact membership in `SecretsSection.aliases` for agent/command secret environments, every MCP server secret environment and each webhook authentication reference. Declared aliases may be reused. Root and every stored body are visited once, including shared and unused definitions; invocations are not expanded. Every graph trigger and MCP server is counted even when it has no secret reference. Alias values are neither read nor copied.

The caller retains document/map ownership. The call owns only its temporary iterators, membership-map reference, scalar counters and existing bounded collector. The returned frozen result owns copied diagnostics and scalar counts; no document, declaration map, provider, cache or traversal object is retained. Real source metadata is reconstructed through `safe_source`, then copied by the existing collector. There are no files, sockets, processes, tasks, transactions or registrations owned by this pass. The existing diagnostics service owns its queue and writer.

Each undeclared reference produces fixed `IR007` with null offset. Typed node/body/environment identifiers are accepted path metadata. MCP server names and MCP environment keys are unrestricted strings and always become `<field>`, even when equal to reviewed schema fields or valid-looking identifiers. This preserves the content boundary without truncating names or falsely identifying a different member. Node source and webhook-owning graph source are revalidated independently; unsafe source is null without ancestor fallback. Independent findings continue and omissions cannot yield valid success.

## Executable checks

| Boundary | Named checks |
|---|---|
| Every reference site and exact membership | `test_secret_alias_membership_covers_every_operational_site`, `test_missing_or_empty_alias_declarations_refuse_references`, `test_declared_aliases_can_be_reused_exactly` |
| Stored traversal and independent errors | `test_stored_secret_references_are_visited_once_without_expansion`, `test_secret_alias_faults_remain_independent`, `test_empty_environments_and_all_trigger_kinds_are_counted` |
| Content and location | `test_secret_alias_diagnostics_preserve_safe_owning_locations`, `test_mcp_map_keys_never_enter_diagnostics`, `test_opaque_content_has_no_alias_obligation`, `test_plain_environment_values_and_nonmembership_policies_are_out_of_scope` |
| Collection and lifetime | `test_secret_alias_diagnostics_preserve_collection_bounds`, `test_secret_alias_validation_releases_inputs_and_results`; site/reuse checks also assert unchanged canonical export and map identity |
| Count-only diagnostics and output independence | `test_secret_alias_logging_is_content_free`, `test_secret_alias_result_survives_unavailable_sink` |

The resource assertions use weak references to actual typed inputs, sources, diagnostics and results, plus temporary-check-object release after collection. Dense faults preserve the existing 1,000-error and 1 MiB caps and count omitted errors. Real blocked/broken pipes preserve validation results, bounded queues and actual writer shutdown. These are blocking behavioral/ownership checks, not numerical process-memory or latency guarantees. No new resource measurement method, repeated CI campaign or timing threshold is introduced.

The pass emits one INFO `ir.secret_aliases.completed` event on `ir.validation`, containing validity and integer counts only. It inherits authorized caller context and leaves context reset to the existing owner. No source, MCP key, alias value/reference, environment value, document, provider or exception text enters the event. Ordinary expected membership failure has no exception path. Unexpected failures remain caller-owned.

## Remaining limits

Membership grants no secret or execution authority. Prefix authorization, provider availability, reserved names, egress/isolation policy, value-like environment detection, namespace/API/database integration, raw-document admission and public execution remain separate work-item-3 and consumer obligations. The pass calls neither provider resolution/listing nor process-environment APIs. Opaque metadata, payloads and ordinary environment values remain uninterpreted. Existing provider configuration stays authoritative for prefixes; this change resolves no open prefix-ownership question. Existing TM-06/TM-07 content protections and ADR authority boundaries remain unchanged. No human-validated `docs/` wording changes.

Focused checks run on the implementation's macOS environment; complete final-head CI must supply the supported Linux/macOS, database, static, scanner and common browser evidence. Static membership touches no metadata database itself. Passing these tests cannot establish full validator, webhook runtime or graph execution readiness.
