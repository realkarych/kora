# ADR-0017: Preserve execution uncertainty until termination and dispatch quiescence are established

- Status: direction and implementation plan accepted, 2026-10-17; document pending owner review
- Reviewed: implementation plan by the project owner, 2026-10-17; document wording pending review
- Amended by: [ADR-0022](0022-inherited-execution-deadlines.md), 2026-10-17 (inherited lifecycle deadlines and expiry); wording pending owner review
- Deciders: project owner
- Absorbs: issue #52
- Supersedes in part: [ADR-0003](0003-scheduler-and-workers-over-http.md) (lease recovery), [ADR-0007](0007-attempt-model-and-retries.md) (replacement eligibility), [ADR-0014](0014-target-identity.md) (hold release), [ADR-0016](0016-trusted-execution-and-publication.md) (recovery authority)
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.
- Tool-count amendment dated 2026-10-19: reconciliation is stated as adding no curated tool instead of keeping a count that `kora_test` (issue #46) has since changed, under [issue #124](https://github.com/realkarych/kora/issues/124); wording pending line-by-line owner review.

## Context

A worker lease expires when the control plane cannot hear the worker, including during a network partition. The runtime and the worker may still run. A stale API epoch prevents metadata writes, but cannot stop a delayed runtime submission, a publisher or a code-host request already dispatched. Retrying with a new attempt id bypasses runtime deduplication on the old id. Treating an unreachable host as terminal `lost` after another lease period therefore permits overlapping executions and releases reservations while work remains possible.

A missing handle is not sufficient evidence either: a worker paused before submission may resume after a lookup returned no handle. Runtime termination does not establish that a separate worker-host publisher or code-host operation stopped. The default must preserve this uncertainty without prescribing a particular runtime, VCS backend, shared worker filesystem or new coordination service.

## Decision

**Persist identities before granting authority.** A non-reused `execution_domain_id` identifies the runtime's durable control-registry lineage, independently of the worker's stable host name. A fresh worker process has a new incarnation id. Before returning any claim that could dispatch preparation, submission or publication, the API records the domain and a dispatcher row binding the worker incarnation and lease generation to an opaque dispatcher id. All such grants remain recorded, including earlier lease holders. Replacing a host or losing its registry creates a new domain; it cannot certify absence in the old one.

**Expiry preserves an unresolved attempt.** Lease expiry sets the orthogonal `execution_state=unknown`, with a reason and last observation. The attempt remains non-terminal, occupies its original reservations and consumes no additional retry count. There is no second-period conversion to `lost`, no time-based assumption that work stopped and no automatic replacement on an unreachable runtime or missing registry. The scheduler persists recovery intent and makes no runtime or worker calls.

**Recover the original attempt.** A fresh claim may observe the original domain and adopt the same live handle. Adoption can read status, logs and checkpoints while an earlier dispatcher remains unresolved; it cannot restore platform write authority merely because the API lease changed. Cancelled or superseded attempts remain claimable for recovery after their run is terminal, with no submission or new publication authority. Late diagnostics and usage remain historical under a fresh recovery lease.

**Retire durably before declaring disappearance safe.** `Runtime.retire(attempt_id, grace)` serializes with submission and finalization in the original durable control registry, installs a permanent per-attempt fence, stops the managed candidate and publisher domains and returns a domain-bound `TerminationReceipt` only after confirmed quiescence. It works without a handle, rejects delayed starts and new effect dispatch, and survives `release`. An unknown outcome is a retryable protocol error, never a receipt. Repeating an already completed operation may return its recorded result read-only; it may not create another effect. Registry loss remains unknown. A normal successful completion also records affirmative runtime quiescence and closes dispatch authority before terminal status; it need not become an infrastructure failure.

**Close every earlier dispatcher.** Each trusted worker closes its local dispatch gate atomically before acknowledging quiescence and only after its in-flight calls have settled or been authoritatively fenced. Acknowledgements bind the original domain, attempt, dispatcher, incarnation and lease generation. The runtime registry accepts monotonic acknowledgements from their authenticated originating workers; a current recovery claim relays them to the API. A runtime receipt alone cannot attest to old worker operations. Any unresolved earlier dispatcher or platform publication request keeps the replacement barrier. An API epoch and a purpose-specific credential are not external-effect fences; a provider must supply actual evidence or the operator must establish termination and closure.

**Retain reservations and acquired target holds.** Any unresolved execution or platform publication recovery (`recovery_required`) blocks new admissions throughout its run; already-running work may finish. Healthy current execution alone does not create this run-wide barrier; live adoption or recovery of the original authoritative immutable prepared/completed checkpoint clears recovery once earlier grants/operations are closed. Before its inherited execution deadline, a non-retired current attempt may finish its stored operation, including change-request creation after a completed push, without restarting candidate execution (ADR-0022). After expiry only read-only reconciliation and closure of already-issued operations remain authorized; an unfinished checkpoint cannot authorize a new push or change request. Cancellation, clear-downstream and result-authority revocation do not release the old attempt's reservations. A previously acquired `target_held_since` clears only when the ordinary run-release condition holds and all blocking execution/publication recovery is settled. Terminal runs with retained holds remain visible. No target key is invented for an unbound run; rebinding and independent-target fairness remain separate decisions (issues #54 and #55).

**Retry waits for evidence.** Manual retry records the ordinary clear/cancel intent, but creates no replacement before the run's barrier clears. A linked rerun is refused with `API002` while the original has any replacement-blocking condition, without an exception. A separately triggered graph run follows the ordinary declared target policy. Once closure is established, infrastructure retry may create a fresh attempt under the existing policy; cancellation remains cancellation. New attempts keep distinct publication destinations. No generic exactly-once or rollback guarantee is introduced for agent or MCP side effects already issued.

**Expose explicit reconciliation.** A namespace-authorized operator with `platform` uses `POST /namespaces/{ns}/runs/{run}/nodes/{node_path}/attempts/{att}/reconcile` with `observe`, `stop` or `confirm_stopped`, an idempotency key and expected recovery version/domain. `observe` and `stop` persist requests for workers; the API does not call the runtime under a database lock. `confirm_stopped` records human attestation that the execution was actually terminated or decommissioned and all old dispatch/publication authority was closed, with a nonempty reason and evidence reference. It is not a force-overlap operation or automatically verified provider evidence. A stale or conflicting request is `API002`; an identical replay returns its original response. There is no assume-stopped action.

**One implementation across surfaces.** Existing reads, events, metrics and console views expose state, domain, version, reason, observations, evidence provenance, retained scopes and permitted actions. `kora reconcile` calls the operation through the shared client; MCP maps it to existing `kora_admin` and adds no curated tool. The console adds recovery information and an operator command, keeping its four MVP mutations. Cost during an observation gap is last-known and `partial`, with its observation time, never fabricated as zero or complete.

## Alternatives considered

- **Retry after a fixed host-loss delay.** Rejected: elapsed time cannot distinguish a dead host from a partitioned or paused one.
- **Rely on API epochs or expiring publication credentials.** Rejected as a generic fence: they do not revoke already dispatched external work; credential issuance and backend rights remain issue #43.
- **Require a renewable runtime execution lease or a watchdog.** Deferred: termination guarantees depend on the runtime and external provider. MVP preserves uncertainty until affirmative evidence is available instead of requiring this mechanism from every adopter.
- **Offer force retry with a warning.** Rejected for MVP: it deliberately creates the overlapping execution the default contract prevents.
- **Release reservations on cancellation.** Rejected: logical cancellation can precede physical termination by an unbounded observation gap.

## Consequences

A permanently unreachable host can hold capacity and a previously acquired target indefinitely until an operator establishes and records closure. This is a deliberate availability tradeoff. Local remains a trusted development/test runtime on Linux and macOS without a sandbox, VM, administrator permission or additional trust switch (ADR-0016); recovery fences provide ordinary cooperative lifecycle guarantees there, not protection against hostile same-user processes. Docker and independent runtimes must prove their declared termination behavior.

The runtime control registry, dispatcher gates and metadata rows implement the contract together; workers still connect outbound to the API and no new service or provider protocol is added. Persisted recovery evidence survives cleanup. Already-issued agent/MCP effects can outlive a runtime; unresolved kora-managed publication remains a replacement barrier, and no rollback of external effects is promised.

This is a design change. Runtime conformance scenarios are implementation gates, not tests that have already run. The repository's documentation CI validates document structure, links, diagrams and existing schemas.

## Where it is applied

- [Runtime and VCS protocols](../../knowledge/specs/protocols.md), including termination evidence, dispatcher acknowledgements and future conformance cases.
- [Data model](../../knowledge/specs/data-model.md) and [scheduler](../../knowledge/specs/scheduler.md), including recovery-only claims, reservations, retry and hold release.
- [API and agent surface](../../knowledge/specs/api-and-agent-surface.md) and [IR retry contract](../../knowledge/specs/ir.md).
- [Functional requirements](../requirements/functional.md), [non-functional requirements](../requirements/non-functional.md), [core boundaries](../technical-introduction.md#5-why-the-boundaries-matter), [console](../console.md) and [roadmap](../roadmap.md).
