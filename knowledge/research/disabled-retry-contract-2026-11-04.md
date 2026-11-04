# Saved disabled retry policies

Date: 2026-11-04. The project owner accepted the persisted interpretation under
[#692](https://github.com/realkarych/kora/issues/692), part of the server engine
[#593](https://github.com/realkarych/kora/issues/593). The
[graph-version contract](../specs/data-model.md#saved-disabled-retry-policies)
defines the exact value. [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md),
[ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) and
[ADR-0024](../../docs/decisions/0024-provider-limits-as-non-agent-failures.md) retain attempt,
immutable history and provider-pause authority.

## Compatibility and ownership

The first-slice contract requires deployment to refuse automatic retries and
provider pauses. Deployment must check every relevant authored and operator
layer, including values that overrides would otherwise hide. An upgrade must
honor a graph version's disabled interpretation. Saving only a count at each
node would leave ordinary body policies and absent dormant fields ambiguous.

The selected binding makes omission explicit through an interpretation ID,
complete node and body maps, and the disabled controls. Authored dormant
operational values remain available in canonical form. There is no default
lookup for dormant fields after deployment. This resolves disabled-policy
semantics independently of enabled-policy precedence or retry arithmetic.
First-attempt workspace selection and any future explicit retry or rerun
remain under their existing operation contracts.

Trusted deployment configuration resolution owns provenance and completeness.
The saved section binds exact values to pinned declarations. The checking
reader can verify that derivation without consulting current configuration.
It cannot reconstruct which operator configuration was supplied to deployment.
Graph ownership, installation validation and all remaining execution-contract
checks retain their own authorities. Original IR, existing graph digests,
historical reads, replay and closure retain the existing storage contracts.

## Implementation gates

- Trusted deployment composition must inspect actual namespace, selected
  runtime/binding, standalone and installation configuration. Test refusal
  of enabling values masked by overrides and incomplete configured sources,
  together with static checking of dormant fields.
- The constructor and reader must bind the exact authored values and complete
  declaration inventory under a shared admitted allowance. Test absent and
  explicit controls, unused bodies, exact numeric kinds and array order,
  altered or reordered maps and wrong interpretation or source associations.
- Before growth, admit the complete trusted configuration, pinned IR and
  policy inventory cumulatively, including per-declaration duplicated values,
  canonical sorting, scratch and retained state. Bound work and concurrency
  at their owners. No numerical resource default is selected here. Release
  metadata read units before interpretation and verify real round trips
  and release on both metadata databases.
- Run, attempt, request-sealing and recovery integration under #593 must
  verify that installation upgrade and changed defaults cannot enable
  automatic scheduling for the saved version. Missing or inconsistent
  sections must refuse new effects while preserving replay and closure.
  [First-slice gates](../specs/first-slice.md#refusal-contract) retain
  the real deployment and worker verification requirements.

## Bounded section construction and checking

[#698](https://github.com/realkarych/kora/issues/698) implements the pure
`engine.graph_retry_policies` section. It structurally checks canonical pinned
IR with the existing graph reader, then visits each root node, each stored body
and each of its nodes once. It explicitly selects the authored operational
fields from typed retry records. Duration and predicate strings, numeric kind
and error-list order are retained. The integer attempt control is explicit,
and the schema-valid zero provider spellings normalize to `"0s"`. Authored
enabled controls, including those in unused bodies, refuse the section.

The builder uses one explicit cumulative canonical-byte allowance. Let I be
the IR bytes, K the sum of encoded name keys retained for sorting, R the one
retained infrastructure-policy template and O the complete emitted section.
Success requires I + K + R + O. Keys are admitted before sort retention, and
each copied template is paid inside O before node construction. Emitted
framing, keys and policy values count in O. The reader also admits S, the
complete supplied saved section. It captures it in canonical text, rejects
cycles, aliases and ambiguous keys, reads the leading interpretation string
from the capture, and compares
the complete text with expected derivation. It returns the captured saved
text only on equality.

The generated section has fixed nesting. Supplied malformed values can have
deeper nesting within their admitted bytes, using existing iterative capture.
Joined fragments and parsed values have call-local ownership. These charges
describe admitted canonical material, rather than Python heap or time.
General numeric parsing retains its existing costs. The complete deployment
owner still admits configured inputs and concurrent work.

`test_disabled_policies_preserve_exact_sources_and_declarations` checks exact
root and body association, order, numeric kinds and omission. The refusal and
capacity tests cover incomplete or changed values, malformed capture, authored
enabling controls, repeated template bytes and exact/one-under admission.
`test_retry_policies_round_trip_outside_metadata_read` uses the real paired
codec and scoped version lookup on SQLite and PostgreSQL. It checks exact
readback, foreign namespace refusal, read-unit release before checking and
final closure after both a matching and a refused check.

The section derives a candidate or verifies the supplied pair. Complete
trusted configuration resolution, CEL syntax/type checks, deployment
publication and execution consumers remain under #593 and their existing
validation/runtime owners. There is no new operational logging surface.
The implementing PR records final CI and independent review evidence.
