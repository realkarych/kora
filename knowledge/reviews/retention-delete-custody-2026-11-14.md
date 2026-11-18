# Retention deletion: presence residual and activation limits

Date: 2026-11-14. Scope: the specification in [#1320](https://github.com/realkarych/kora/issues/1320), under its [approved plan and choices](https://github.com/realkarych/kora/issues/1320#issuecomment-6776724938). This record documents the selected residual; it is not runtime qualification or a threat-model amendment. The [owner resolution of 2026-11-15](https://github.com/realkarych/kora/issues/1320#issuecomment-6884270282) preserves the existing public upload refusal and requires the direct deletion-state signal to be recorded explicitly.

## Shared address and private ownership

[Retention delete custody](../specs/data-model.md#retention-delete-custody) coordinates the exact physical storage identity and full payload digest across aliases and namespaces. A pending generation refuses new eligibility at that address until all issued effects have settled or are authoritatively unable to execute later. Public upload acceptance preserves the [existing API response](../specs/api-and-agent-surface.md#worker-route-bodies-issue-139): `API002`, `retryable: true`, `details.reason: payload_deletion_pending`. It discloses no digest, deletion generation, incarnation, owner identity, namespace or creation attribution.

The reason explicitly reveals pending deletion at the supplied physical address to an otherwise authorized uploader of known bytes. This is a direct deletion-state signal across namespaces; refusal duration provides an additional inference about work at that address. Neither signal confirms a particular owner, authorizes a read or proves that another namespace still retains the bytes. The owner selected preservation of the existing API and accurate recording of both signals; this change selects no concealment mechanism or new response behavior.

The existing [threat model](../../docs/threat-model.md) continues to govern:

- TM-09 covers delayed effects, lost responses and the two admission commit orders. Timeout, cancellation, restart and absence do not permit reopening.
- TM-10 requires finite charged deletion capacity and actual closure before release. A stuck effect may keep its address and slot unavailable; no unconditional cleanup-time guarantee is made.
- TM-11 keeps physical identity distinct from authorization. Every applicable owner and hold must be checked, including legacy ownership and aliases outside the caller's namespace.
- TM-06 and TM-07 preserve content-free refusals and diagnostics and the privacy of owner identity and creation attribution. The existing public reason reveals deletion state; that direct signal and refusal duration remain explicit limits on cross-namespace presence confidentiality.

## Implementation and evidence boundary

The specification adds no executable cleanup, schema or provider behavior. #216 owns complete owner admission, crash-durable metadata authority, issuer closure, exact commit reconciliation, native writer/recovery implementation and the real SQLite/PostgreSQL integration evidence. The local provider's retained receipt, release-time epoch advancement and compacted-operation release reply are dependencies of its selected closure proof. They do not establish core durability, and they are not newly imposed on all storage providers.

Before activation, the implementing issue must qualify the named restart and stale-delete scenarios, both admission orders, saturation progress, generation-bound owner expiry and staging handoff. An authoritative no-effect result is a separate qualification case; current local delete success evidence does not establish that case. Unverified ownership, authority continuity, durability or effect closure keeps deletion disabled or its existing custody charged.

Revisit the residual when storage integration changes the public refusal, acknowledgment cadence, owner-admission coverage or physical deduplication domain, or when an enabled deployment requires stronger cross-namespace presence concealment. Any new confidentiality guarantee or response behavior requires its own explicit owner decision and matching executable evidence.
