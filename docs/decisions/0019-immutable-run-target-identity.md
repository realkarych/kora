# ADR-0019: Immutable run target identity and atomic acceptance of target-source results

- Status: direction and implementation plan accepted, 2026-10-17; document wording pending owner review
- Reviewed: implementation plan by the project owner, 2026-10-17; document wording pending line-by-line review
- Deciders: project owner
- Resolves: issue #54
- Amends: [ADR-0014](0014-target-identity.md) (binding and historical attribution)
- Related: ADR-0002, ADR-0006, ADR-0007, ADR-0008, ADR-0017, ADR-0018
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

## Context

A run's target is both an admission key and the key through which its history is projected. Replacing an output could previously change that key while preserving an acquired hold. A run holding Q could become a holder of R without acquiring R, and its earlier Q approvals and costs would appear under R. Freezing only after acquisition would still allow historical reassignment before the first hold.

The output boundary also needs one meaning. Upload validates artifacts before completion decides whether a source succeeded. Rejecting only a key change at completion leaves companion outputs available to consumers using failure-compatible trigger rules such as `all_done`.

## Decision

**A run binds at most one target key for its entire lifetime.** A valid present parameter binds at run creation. An absent optional parameter leaves it unbound; an invalid present value is a validation error, not absence. An output-derived key binds on the first successful acceptance of the declared source's complete result. Once non-null, the key never changes or clears, including after cancellation, clear-downstream, failed-run retry, settled hold release or source-payload expiry. Equality is exact and case-sensitive under the existing key grammar. Identity, delivery and attempt remain distinct.

**Accept the source's whole output set atomically.** The declaration names one top-level `graph.nodes` ID, including an exported subgraph output or a schema-resolvable string selected from collection output. It cannot name a dynamic body instance. All outputs of a physical target source, including implicit session/result artifacts, are stored as `staged`, unavailable to graph input resolution. Other nodes keep existing upload behavior. Under the run lock, completion rechecks current authority, ADR-0017 closure, the complete valid output set from that production and the selected key. It binds the first key or compares the existing one, then promotes the output set and completes the source in the same transaction.

Scheduler-only sources use the same acceptance decision without a physical attempt. Wrapper exports own logical artifact-version rows and lineage to body results; they may share immutable bytes but never rename or invalidate the body artifact to reject an export. Wake, approval, collection and wrapper completion recheck their exact current wait/request, iteration, bindings and child versions before acceptance.

**A different valid key rejects the production.** The source fails with existing `config_error`, named internal code and reason `target_identity_conflict`, identifying the bound/proposed keys and source artifact. None of its outputs becomes current. Valid companion outputs remain historical; the conflicting output is invalid. The conflict is never automatically retried, including by the source wrapper's own retry or `skip_failures` handling. Missing, non-string, malformed or incomplete source data follows `artifact_invalid`; it never means to keep the old key while accepting the other outputs. The comparison occurs at final acceptance, after ordinary physical closure, rather than inventing an upload-time stop state. Failure handlers may run, but cannot consume rejected outputs as current.

**Replays cannot change acceptance.** Each physical source output is write-once by `(attempt_id, output_name)`. A complete payload digest, independent of a type's selected digest fields, survives retention in its descriptor. After current namespace/service/epoch authorization, matching replay returns the recorded artifact state read-only; changed content conflicts without replacing the first result or changing a settled outcome. Replay never renews retention or restores an expired owner. New late writes cannot become current merely because an old current-attempt pointer remains. Cancelled or superseded work is historical and cannot fail a replacement source. Multipart storage precedes the short metadata transaction and grants no authority.

**Binding is not acquisition.** Initial binding changes only this run and acquires no hold. The existing admission scope takes the hold; ADR-0017 governs release and recovery. API/scheduler source mutations serialize under the run lock. Admission takes its ordered scope locks and reads fresh counts before acquisition. PostgreSQL uses READ COMMITTED reads after the locks, or an explicitly retried serializable equivalent; SQLite keeps its single-writer transaction discipline. No target transfer, global holder-uniqueness index or additional run lock under a target lock is introduced.

**Keep history and public recovery explicit.** The first `run.target_bound` event records the parameter name or exact accepted artifact/version selector. Same-key rediscovery writes no second initial-binding event. Initial discovery and failed/rejected proposals remain costs of that run; first binding associates previously unbound history once, and subsequent retries cannot move it to another key. Payload expiry leaves the compact key and origin readable. Existing clear-downstream invalidates only its ordinary closure, preserving independent old evidence.

The conflict is an asynchronous source failure visible through existing node/attempt/event reads, CLI and MCP. A retry request cannot synchronously reject a key not yet discovered. Explicit manual retry may produce the original key. Another target needs a fresh run, optionally the existing linked `rerun`, subject to all original replacement blockers. Rerun consumes no old artifacts and transfers no hold. It rediscovers independently; reliable selection requires a graph-declared, deterministically enforced selector or a suitable new graph version, not a new trigger field.

## Alternatives considered

- **Freeze at first acquisition.** Rejected: earlier history can still move between targets.
- **Freeze at first valid fragment upload.** Rejected: a later missing/invalid companion or producer failure would still bind the run. Successful complete-result acceptance gives discovery one boundary.
- **Demote companions when rejecting a changed key.** Possible with additional visibility and irreversible-rejection invariants. Local staging makes the whole-result acceptance rule explicit and avoids transiently current rejected results.
- **Transfer the target inside a run.** Deferred: correct history requires attribution boundaries as well as atomic release/acquire, re-admission and unresolved-work handling. A linked new run already separates both histories.
- **Add a target-conflict error class or a target catalogue.** Rejected for MVP: existing `config_error`, artifact states, events and read projections suffice.

## Consequences

- An incorrectly selected first target cannot be corrected in place, even before its first hold; create a new run instead.
- Staging is local to the declared source, introduces no new attempt state, service or dependency, and follows existing content retention/access rules. Live logs and usage remain available through their existing streams.
- Target identity is declared metadata, not proof of which business object an agent actually touched. Discovery, already admitted unbound work and a wrapper's internal body may already have effects. Rejection does not roll back remote branches or arbitrary agent/MCP actions.
- Target-dependent runtime work must depend on the source to be admitted after binding. This record does not add holds for scheduler-only approval paths, change incoming `allow` behavior or decide deferred-run capacity/fairness (issue #55).
- The unreleased IR remains version 1.0. Runtime/database safety remains a future implementation contract; documentation validation alone cannot prove it.

## Where it is applied

- [FR-6.11](../requirements/functional.md), [ADR-0014](0014-target-identity.md)
- [data-model.md section 12](../../knowledge/specs/data-model.md#12-immutable-run-target-identity-adr-0019): lifecycle, ownership and future acceptance matrix
- [scheduler.md](../../knowledge/specs/scheduler.md): completion, wakes/collection, locks, target admission and retry
- [ir.md](../../knowledge/specs/ir.md), [dsl.md](../../knowledge/specs/dsl.md), [IR schema](../../knowledge/specs/ir.schema.json): source resolution and first successful acceptance
- [api-and-agent-surface.md](../../knowledge/specs/api-and-agent-surface.md), [protocols.md](../../knowledge/specs/protocols.md): reporting, asynchronous errors, inspection and recovery
- [core objects](../technical-introduction.md#2-the-objects-that-move-through-it), [scheduler components](../../knowledge/architecture/detailed.md#components-inside-the-scheduler), [console pages](../../knowledge/design/console/pages.md), [console states](../../knowledge/design/console/states.md)
