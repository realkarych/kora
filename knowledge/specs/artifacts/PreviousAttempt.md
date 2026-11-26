# PreviousAttempt revision 1

The project owner approved this detached artifact revision and its implementation plan under [issue #1860](https://github.com/realkarych/kora/issues/1860#issuecomment-7148352440). It represents the outcome of the immediately preceding subgraph iteration under [FR-2.7a](../../../docs/requirements/functional.md) and [ADR-0007](../../../docs/decisions/0007-attempt-model-and-retries.md). The model validates and owns one immutable canonical value. Runtime selection, storage, delivery and authorization retain their separate owners under [ADR-0018](../../../docs/decisions/0018-immutable-attempt-history.md).

## 1. Closed logical value

```json
{
  "artifacts": {
    "check.result": {"artifact_version_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW"},
    "implement.change": {"artifact_version_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
  },
  "error_class": "attempt_failed",
  "reason": "SQL result mismatch",
  "subgraph_attempt": 1
}
```

All four root fields are required. The [draft-2020-12 schema](PreviousAttempt.schema.json) has no parent. Its member rules are supplemented by the checked byte and exact native value rules below.

| Member | Meaning and revision limit |
|---|---|
| `subgraph_attempt` | The predecessor's subgraph iteration, a strict integer from 1 through 99. The authored retry limit is at most 100, leaving room for a successor. The eventual writer checks the effective saved limit and exact iteration. |
| `error_class` | One of the closed IR error-class spellings frozen by value in the schema. The standalone validator establishes membership. The first planned producer handles only explicit `attempt_failed`. |
| `reason` | Exact Unicode scalar text, including an empty string. The first planned producer requires an authored and successfully evaluated reason on the selected `$fail_attempt` route. The complete canonical byte limit bounds the string. An unescaped ASCII reason can occupy at most 1,048,503 bytes in the smallest legal envelope. |
| `artifacts` | A map of zero or more unique `node_id.output` keys to closed one-member reference objects. Each side of the dot is an IR NodeId of 1–64 ASCII characters and the full key spans 3–129 bytes. Keys name outputs of direct nodes in the pinned body, including internal outputs. A nested wrapper export, when separately supported, is an output of that wrapper. |
| `artifact_version_id` | One canonical 26-character uppercase Crockford ULID with first character 0–7. The version owner carries its type, contract, logical and applicable content identities. An accepted AgentSession can be referenced even when no physical transcript was captured. |

The root and references accept exact native JSON dictionaries. Their keys and all text values have exact string type. Booleans, floating values and exponent tokens are outside the iteration domain. Arrays, null, extra members, malformed UTF-8, BOM, duplicates after escape decoding and lone surrogates are refused. U+0000 in the reason remains part of the value and is escaped in canonical bytes. Defaults, normalization and truncation would change the value and are excluded.

Raw and canonical bytes each have a 1,048,576-byte ceiling. The minimum empty envelope takes 73 bytes and the minimum reference entry takes 58 bytes plus a separator after the first entry. The whole-body ceiling therefore gives a conservative inventory bound of 17,771 entries. The byte scan rejects arrays and checks at most three container levels, with member ceilings of four in the root, 17,771 in the sole inventory and one in each reference. It also charges malformed and repeated members before general JSON decoding. The integer hook accepts tokens for 1 through 99 before converting them. Native admission checks exact types, key widths and canonical work before copying or sorting. The codec owns only its invocation-local state. Integration admits source buffers, scratch and retained outputs separately and keeps borrowed native dictionaries stable, including ownership of their backing allocation.

## 2. Canonical identity and detached Python boundary

The [immutable revision descriptor](PreviousAttempt.revision-1.json) pins the schema file bytes, the validation identity `kora.previous-attempt-validation.v1`, the existing `kora.structured-json.v1` canonicalization and `kora.json-payload.v1` for root selection and identity summary. The root selection is `[""]`. Exact canonical bytes use UTF-8, Unicode-code-point object key order, compact separators, decimal integer tokens and the existing JSON escapes, with no BOM or final newline. Reordering members preserves identity. Changing any semantic field or any version ID changes the canonical identity, including a change between versions with equal child content. The selected digest and storage digest remain independent roles. Both hash the entire canonical byte string, and storage uses the `sha256:` prefix.

Construct `kora.ir.PreviousAttempt(value)` from admitted native values. The value owns immutable canonical bytes and supplies a fresh identity-summary value through `.value`; `.canonical_bytes()` returns the exact bytes and `.payload_digest()` returns the prefixed complete SHA-256. In `kora.ir.previous_attempt`, `parse_previous_attempt_json` requires canonical bytes, and `parse_retained_previous_attempt` additionally compares the expected full digest obtained from the trusted owner. An equal digest alone supplies no owner or content access proof. `previous_attempt_revision_descriptor` returns a detached snapshot and `require_previous_attempt_revision` checks the complete published descriptor.

One shared `ContractViolation` carries a fixed message and an empty path. `PA001` indicates excessive raw bytes, `PA002` invalid lexical or JSON encoding, `PA003` an invalid logical value, `PA004` excessive canonical bytes, `PA005` an incompatible published descriptor and `PA006` a noncanonical retained representation or failed expected-digest comparison. Refusals, their exception chains, codec traceback locals and the model representation exclude reason text, keys, version IDs, digests and bytes. The detached API has no logging operation. Future ordinary events and operational diagnostics carry safe fixed facts and never copy the evaluated reason.

## 3. Integration boundaries

On the first iteration the supported optional `$previous_attempt` input is absent. Only declared consumers receive it on a successor. The authoritative producer freezes the exact eligible, accepted output versions before clear, including versions with now-unavailable content. An omitted member means that the complete checked inventory proves no eligible accepted result for that slot. An empty map is valid when no such outputs exist. An accepted artifact whose value is JSON null remains a version reference. Unknown ownership, expiry and reassignment of mutable current pointers do not produce omission.

The Scheduler transition checks the namespace, run, pinned graph/contract, concrete wrapper and body occurrence, iteration and saved policy. It owns `artifact_version` with `output_name = previous_attempt` and no physical attempt, its exact lineage and the successor's declared immutable input bindings. Normal supersession preserves the accepted historical source and its availability rules. An owner or version mismatch refuses even with equal payload bytes. Specific write keys, granted scopes, bounded source acquisition, replay and restart evidence belong to that integration.

Canonical bytes are authoritative at every size. The generic decoded 64 KiB inline representation cannot establish this property for arbitrary strings, including NUL in PostgreSQL JSONB. An owning implementation must select a lossless native placement under the existing content descriptor and prove exact bytes on both metadata databases. This revision has no locator or migration. The archived envelope and its summary or metadata projection use the shorter of the run's saved transcript and artifact-payload retention periods. External disclosure of the reason or equivalent feedback uses the existing `transcripts` capability and current namespace/object authority. History retains IDs, immutable contract, digests and lineage. Existing active and recovery holds apply only to needed exact owners.

The wire entries are references. The consumer-facing `artifacts['node.output']` route resolves a selected earlier typed value under its exact saved owner and applicable view. CEL uses the existing saved metadata and original child cuts under the pinned path set and [IR bounds](../ir.md#76-data-bounds-cost-and-evaluation). It keeps child bytes with their original owners. AgentSession's metadata-only view can remain available after loss of its primary transcript. A missing matching cut is unavailable, and an over-bound cut at production fails with `artifact_invalid`. Jinja and workspace selection use independently admitted typed reads. A selected `VcsRef` supplies its recorded commit. Required unavailable content refuses before execution effects, with `config_error` internally or `API002/execution_inputs_unavailable` for manual retry. Availability and access come from the exact saved owner, independently of whether identical content exists elsewhere. Same-attempt recovery of a sealed request retains its own already pinned content and holds. A new attempt cannot recover absent inputs from earlier request copies.

This publication supplies the detached model and packaged schema. The live type and schema lookup tables retain their current support, including `IR022` and first-slice `IR028`. The Scheduler, CEL path/view, Worker request, content and retention integrations must qualify before graph admission or retry is activated.

## 4. Evidence

The [canonical vectors](PreviousAttempt.vectors.json) pin exact bytes and complete digests. `tests/ir/test_previous_attempt.py` exercises reference identity, encoding, exact limits, support refusals, invocation-local ownership and safe diagnostics. The synthetic SQL component example uses the existing CommandResult, VcsRef and CEL boundaries. The test supplies the iteration and reference selection, with actual transition and delivery evidence owned by the second-slice integrations. `test_type_schema_catalogue_is_published_unchanged_in_core_wheel` compares the packaged schema with the published bytes.
