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

The contract is specified here. Trusted source resolution, real construction
and checking, and execution consumer activation are pending under #593 and
the existing validation and runtime work items. Their executable evidence
belongs to the implementing changes.
