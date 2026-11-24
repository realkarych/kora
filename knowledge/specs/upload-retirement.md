# Upload retirement and deletion settlement handoff

Status: design under [#1581](https://github.com/realkarych/kora/issues/1581), 2026-11-15. An owner [approved the specification plan](https://github.com/realkarych/kora/issues/1581#issuecomment-6893519377). The native records, indexes and transitions below remain pending implementation. Publication activates no cleanup, request-input materialization or periodic retention.

## 1. Scope and authority

This contract defines when an original refused-upload owner consumes a deletion settlement and releases its reservation, and when deletion may forget that evidence. It applies to every participating typed staging owner, including prepared content and API-created request inputs. A later upload admitted after reopening must not retain an earlier deletion's evidence indefinitely.

[API Uploads](api-and-agent-surface.md#worker-route-bodies-issue-139) governs authorization, truthful promotion attribution, retirement and reservation accounting. [Execution content](execution-content.md#22-upload-custody) defines typed upload/operation/finalizer ownership. [Data-model retention](data-model.md#10-retention-dd-4) governs every retaining owner and hold; its [delete custody](data-model.md#retention-delete-custody) retains the immutable intent and write-once settlement. [Qualified custody authority](execution-custody-authority.md) supplies original-COMMIT, issuer and restore prerequisites. The [storage protocol](protocols.md#7-artifact-storage-fr-44) owns actual byte effects, retained outcomes and acknowledgment.

[ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md), [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md), [ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md), [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) and [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md) govern the unchanged closure, history and deadline obligations. The existing [TM-09/TM-10/TM-11 controls and TM-07 retention obligations](../../docs/threat-model.md) apply. No new actor, provider ABI, public field, retention period, capacity, pacing value or verification policy is introduced.

An address is trusted physical `store_id` plus full `payload_digest`, across aliases and namespaces. Every use also binds the exact positive native `fence_incarnation`. A generation floor is bookkeeping for evidence consumption, never provider-dispatch, existence, eligibility, deletion, durable-commit or issuer-closure authority. Unknown ownership or closure keeps its original custody and charge. Missing or unverifiable legacy coverage blocks activation for the affected address or domain.

## 2. Native staging facts

Keep each owner in its existing typed relation. The retained address is the actual destination admitted before promotion: the prepared address when transformation changes it, otherwise the admitted source address. Its exact address/incarnation and original promotion operation remain immutable for that owner. Request-input preparation has no declared-address admission and acquires its retained address only when the actual prepared destination is known.

| Native field | Meaning and constraint |
|---|---|
| `retained_admission_generation` | Nonnegative signed-64 `Count`, captured from the fence's `last_deletion_generation` atomically with actual retained-address admission, while no deletion is pending. Immutable for that owner/address; distinct from provider epoch, content generation and worker grant. |
| `cleanup_generation_floor` | Same native domain, initially equal to `retained_admission_generation`. It never decreases or changes the admission fact. Advance only to an exact authoritative deletion settlement whose consequence this owner durably consumed. |
| `storage_closure` | Native `pending` or `closed`, initially `pending`. `closed` is an irreversible projection of actual original closure evidence, never a caller-supplied proof flag or authority to issue useful storage work. |

The two generation fields are absent exactly when the owner has no admitted retained address. No NULL-to-zero backfill makes historical custody eligible. A clock, missing object, current fence counter or caller assertion cannot advance the floor. Native reads and writes retain the existing bounded acquisition and strict domain rules.

`storage_closure = closed` requires all of the following:

1. Useful issuance for this original owner is permanently closed.
2. Every admitted finalizer, reader, dispatcher and native job has actually settled or is authoritatively excluded from executing later. This includes an authorized invocation paused before provider entry or receipt registration.
3. Where a provider stage existed, its original retirement and exact release acknowledgment have completed, with the original operation set, outcomes and revision identities retained under their existing compaction rules.
4. The complete evidence is committed through the qualified original-COMMIT path. Lost COMMIT or acknowledgment response, or unknown actual closure, leaves the owner pending until the exact original outcome is resolved.

A never-dispatched operation needs affirmative original non-dispatch and closure evidence. A provider's private pending-receipt registry cannot prove absence of an admitted core invocation that has not entered the provider. A logical gate, cancellation, deadline or process restart alone proves none of the required actual closure.

Ready-promoted request-input rows derive closure from their recorded earlier stage release and actual core closure. They do not retire a new stage. They remain `staged` retaining owners until metadata-only sealing or refusal; `closed` never removes the independent staged-owner protection. Once closed, an owner cannot reopen useful storage work. A new upload requires a fresh owner and admission.

## 3. Indexed protection and evidence dependencies

### 3.1. Protection before deletion admission

Every admitted declared/source or retained address of a `staged` row remains protected. An `expiring` or `accepted` row also retains that protection at every admitted address while `storage_closure = pending`. Marking an owner expiring never removes protection while it could still create or modify bytes.

Each applicable typed staging relation provides separate address-leading partial indexes for its declared/source and retained projections. Each index orders its native fields as `(store_id, payload_digest, fence_incarnation, upload_primary_key)` and includes exactly the rows satisfying `state = staged OR storage_closure = pending` with that address present. An absent address has no projection; equal source and retained addresses confer the same protection without an extra owner. Include zero-byte reservations and unknown promotion attribution.

Under the shared address guard, deletion admission proves no protected row exists through each applicable exact-address/incarnation index, across all namespaces and aliases. It also performs all existing durable-owner and hold checks. It cannot scan closed expiring history and inspect issuer/receipt bodies to find a remaining blocker. A legacy row missing complete native projection or evidence is an activation refusal, never silently excluded from coverage.

Only the qualified closure transition writes `closed`, under the complete owner address guard set. Its original evidence stays owned and reconstructable until final owner completion. Thus the deletion later consumed by cleanup is admitted only after every original staging owner's last possible creation at that address has durably closed. Matching address, incarnation and generation alone does not establish that ordering.

### 3.2. Original settlement consumers

Each typed staging relation also supplies a separate partial index on `(store_id, retained_payload_digest, retained_fence_incarnation, cleanup_generation_floor, upload_primary_key)` for `expiring` rows with an admitted retained address. Zero-byte and unknown-attribution rows participate; a reservation-size or optimistic attribution filter cannot hide a dependency.

For an intent at exact address/incarnation and deletion generation `g`, an original staging dependency remains precisely when an applicable indexed expiring owner has `cleanup_generation_floor < g`. Constrain address and incarnation by equality and the floor by one native index range. Processing uses finite admitted keyset pages; compaction uses indexed existence checks. Neither positive nor negative lookup may scan then filter two independent generation ranges or walk already consumed history. The existing owning cleanup delivery supplies finite page/byte admission before acquisition; this design selects no new numerical value.

There is at most one live delete intent per incarnation. A later generation cannot be admitted before the previous intent compacts. Once the pending pointer clears, a new upload captures admission generation and floor `g`; even if later refused, it cannot join the old intent's dependency set. It still independently protects the address while staged or closure-pending. A fresh fence incarnation cannot consume an old incarnation's settlement even when its numeric generation repeats.

## 4. Transitions and original recovery

Original upload admission, truthful promotion attribution, finalizer ownership and provider dispatch retain their governing contracts. No metadata resource spans a provider call, parsing or reader closure.

| Transition | Required original evidence | Atomic metadata consequence |
|---|---|---|
| Whole-owner refusal | Original owner and refusal under the existing authority checks; resolve its original COMMIT before relying on it. | Mark `expiring` and close useful issuance. Preserve all protective admissions and pending actual closure. An already proven closed ready-promoted row does not reopen. |
| Actual storage closure | Complete section 2 evidence, including pre-entry invocations and exact stage release where applicable. | Commit `storage_closure = closed` under the complete address union. An expiring row then ceases to block deletion admission; its reservation and settlement dependency remain. |
| Proven no-create completion | Actual storage closure and authoritative original promotion evidence proving no creation. Unknown attribution is insufficient. | Remove the original row and release its exact reservation in one transaction, once only. |
| Independently retained completion | Actual storage closure, a currently eligible accepted durable non-staging owner under the complete [indexed eligibility contract](execution-content.md#24-eligibility-and-indexes), and no unresolved deletion authority. | Recheck eligibility and remove the original row/release its reservation atomically against owner removal, expiry and deletion admission. Renew no other retention or access. |
| Created-object deletion consumption | Actual storage closure; exact original intent/settlement/operation binding; matching retained address/incarnation; generation `g` above the floor; deletion admitted after original creation closure; authoritative `DeleteResult` with every issued deletion effect closed under delete custody. | Remove the original row and release its reservation together. Replay cannot decrement the reservation again. |
| Authoritative no-effect consumption | Same exact binding and creation-closure ordering, but the retained settlement contains authoritative `NoEffectFailure`. | Set the floor to `g`; preserve `expiring`, original promotion facts and the entire reservation. This consumes evidence without claiming content deletion, no-create attribution or cleanup success. |
| Unknown or unverifiable outcome | Missing, pending, mismatched, conflicting or otherwise unverifiable original evidence. | Keep original identity, floor, guards and charges. No replacement owner, invented outcome or generation advance. |

An accepted upload has already released its core reservation at acceptance; its stage/provider resources remain separately charged until actual closure. Removing its remaining staging row never releases that reservation again. Its pending closure still protects each admitted address.

Delete-intent compaction follows the existing expiry/reopening, exact provider acknowledgment and qualified-commit rules, then proves absence of every original staging dependency through section 3.2. Keep the settlement until all those owners consume its consequence. A fully consumed no-effect intent can compact while original uploads remain charged for a future generation. That retry keeps existing finite pacing; there is no permanent per-generation ledger. Fence compaction separately remains forbidden while any retaining owner, hold or unresolved stage/charge still needs it.

### 4.1. Complete owner locks

Delete-intent admission, settlement, payload expiry and compaction retain their one-exact-address writer units. Upload closure, evidence consumption and row removal instead require the owning contract's complete bounded declared/source/retained address union, even when only one address supplies the consumed deletion settlement.

Load the bounded original owner inventory, acquire the complete union in canonical order and recheck it before touching owner/accounting rows. Discovery of a new required address rolls back and reacquires the complete set; never acquire an earlier class or address late. Preserve the existing capacity/scope/address/staging/accounting order and the original absolute deadline across waits. Partial acquisition authorizes no owner transition. Row deletion and exact reservation accounting share the same transaction.

### 4.2. Crash and replay consequences

| Interruption or ordering | Required recovery |
|---|---|
| Refusal commits while an authorized promotion is paused before provider registration | Keep closure pending and all address protection. Another cleanup cannot admit deletion until that original invocation actually settles or is authoritatively excluded. |
| Closure COMMIT or release acknowledgment loses its response | Resolve the exact original operation/commit. Unknown closure stays pending; do not manufacture closed state from object absence or a fresh receiver. |
| Deletion settles before a possible original creation has closed | The ordering is invalid for consumption, even if address/incarnation/floor match. Keep custody; an old delete result cannot authorize charge release. |
| Reopening commits before original cleanup consumes the settlement | Keep the settlement and old dependency; new admission records floor `g` and cannot join it. |
| Original cleanup consumes before a competing new admission commits | Preserve exactly-once removal/accounting under the same guards. The new admission uses its own fresh identity and current generation. |
| No-effect floor update loses its COMMIT response | Reconcile the original update. The floor is either the earlier committed value or exactly `g`; the full reservation remains in either case. |
| Row removal/accounting COMMIT loses its response | Resolve the original row and native commit outcome. Both changes committed or neither did; do not perform a second decrement or allocate replacement custody. |
| Restart, restore or receiver reconstruction | Preserve original address/incarnation, operation, floor and closure evidence under qualified authority. Unknown continuity refuses effects and compaction. A new receiver cannot refresh an old invocation. |

The public deletion-pending refusal remains `API002`, `retryable: true`, `details.reason: payload_deletion_pending`, with its [accepted presence/timing residual](../reviews/retention-delete-custody-2026-11-14.md). No digest, generation, owner, namespace or closure detail is exposed. [Observability](observability.md) keeps diagnostics bounded and content-free; logs cannot supply transition authority or delay actual cleanup. Existing retention events and activation ownership remain unchanged.

## 5. Activation and verification ownership

[#1640](https://github.com/realkarych/kora/issues/1640) owns the first request-upload integration of these typed fields/indexes, qualified writers, reservation transitions, finite processing and handoff to delete custody. [#216](https://github.com/realkarych/kora/issues/216) owns broader retention integration. [#1581](https://github.com/realkarych/kora/issues/1581) specifies the staging handoff, and [#1580](https://github.com/realkarych/kora/issues/1580) specifies the journal, native COMMIT and issuer requirements. The actual qualified producers and physical crash/restore evidence remain implementation prerequisites. Existing fence, managed-lock and local-provider components do not establish an upload lifecycle or authorize cleanup by themselves.

[#1640](https://github.com/realkarych/kora/issues/1640) supplies the real upload-to-ready, expiry, original closure and guarded cleanup, with the existing [physical crash/restore qualification](execution-custody-native.md#6-qualification-method-and-activation-evidence) before custody activation. [#1853](https://github.com/realkarych/kora/issues/1853) supplies [accepted-request eligibility, availability and the shared hold and deletion interlock](execution-content.md#24-eligibility-and-indexes). #1453 supplies the actual accepted request and original Worker read/recovery. The [joined request-owner case](execution-request.md#shared-address-request-owner-acceptance) proves independently retained completion and original replay before the first retained use or second Claim. VcsRef activation with #216 and its actual permanent accepted producer requires `test_refused_upload_shared_address_releases_staging_reservation` and every condition in the [API contract](api-and-agent-surface.md#worker-route-bodies-issue-139). Before activating the first typed artifact producer that supplies independently accepted same-address owners with applicable retention, #216 and that producer also prove both orders of typed acceptance versus another owner's retention under `test_refused_upload_bytes_are_reclaimed`.

The previous 700–950-line component estimate is unverified until a complete implementation inventory includes actual staging, issuer and cleanup producers. Runtime delivery requires its own approved plan within the production-size limit, or a separately approved bounded exception. Full periodic retention and graph-file publication remain deferred independently; this contract does not resume them or exempt legacy/all-owner coverage.

The following cases are future executable obligations, not tests run or guarantees implemented by specification publication. Each metadata case uses real SQLite and PostgreSQL with real local storage, deterministic constructor-injected barriers and supported platform coverage. Success, failure, cancellation, shutdown and restart exercise actual resource release at the owning boundary.

| Named future case | Required observations |
|---|---|
| `test_refused_upload_cleanup_consumes_original_delete_settlement` | Exact original identity/attribution and `DeleteResult`; zero-byte and unknown-attribution owners; both reopening/cleanup commit orders; duplicate recovery and lost COMMIT; source/retained addresses in both key orders; failed complete-union acquisition causes no partial removal or decrement. Independently retained completion checks current eligible ownership atomically; accepted-row removal never decrements twice. |
| `test_delete_waits_for_expiring_upload_creation_closure` | Pause an admitted promotion before provider registration, mark it expiring, and attempt independent deletion. Protection remains until actual closure. Also cover a provider-registered delayed effect, pending accepted-row closure and a ready-promoted request-input competitor; closing a logical gate or passing a deadline is insufficient. |
| `test_new_upload_does_not_join_previous_delete_cleanup` | Reopen after settlement, admit/refuse another upload of the same bytes, and compact the old intent only after original consumers finish. The later floor cannot retain old evidence. Stale issuers and a fresh incarnation with a repeated numeric generation cannot consume or execute old authority. |
| `test_cleanup_no_effect_consumption_preserves_charge` | Consume an actual authoritative no-effect result, retain full charge and promotion facts, compact the consumed intent, then retry under existing pacing. Repeated no-effect generations and lost floor-update response neither release the reservation nor grow a permanent generation ledger. |
| `test_cleanup_dependency_lookup_is_index_bounded` | Growing closed, later-admitted and already-consumed same-address history does not increase rows inspected by exact live-blocker or compaction existence checks. Exercise positive and negative cases, zero-byte/unknown rows, all applicable relations and both databases. Keyset processing remains finitely admitted; genuine unresolved owners remain charged. |

The current local provider's successful deletion does not qualify a future `NoEffectFailure` producer. Implementation must name and exercise the actual supported no-effect source, or retain that case as pending. Native physical crash qualification, original-COMMIT producers, runtime SQL plans and contention/cancellation resource evidence remain separate pending obligations. No latency, RSS, throughput or leak-freedom guarantee follows from these design indexes.

Specification publication requires changed-file Markdown lint, offline links/anchors, whitespace validation, independent correctness/security/performance review of the complete change and one complete successful final-head CI. It adds no browser review, mutation campaign, numerical experiment or additional verification stage.
