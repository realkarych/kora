# ADR-0021: Observe external systems outside database transactions and apply only to the current owner

- Status: direction and implementation plan accepted, 2026-10-17; document wording pending owner review
- Reviewed: implementation plan by the project owner, 2026-10-17; document wording pending line-by-line review
- Deciders: project owner
- Resolves: issue #56
- Amends: [ADR-0008](0008-content-bound-approvals.md) (observation and channel-delivery lifecycle)
- Related: ADR-0002, ADR-0003, ADR-0005, ADR-0011, ADR-0013, ADR-0017, ADR-0018, ADR-0019, ADR-0020

## Context

A sensor, approval channel or code host can respond slowly. Calling it while holding a database transaction or run lock delays cancellation, retry and other decisions. Calling it after releasing locks creates another obligation: an answer requested for an old wait must not resolve its replacement. Approval-channel writes also need recovery when a remote request succeeds but its response is lost.

## Decision

**Separate claiming, calling and applying.** In a short transaction, save an observation token and snapshot of the exact wait/request or start binding, inputs, saved contract and integration configuration. Release all database transactions, connections and run/scope locks before resolving content or credentials and invoking the provider. Reacquire the existing locks and apply through the existing transition owner only if the token, owner, relevant bindings, current authority and expiry still permit it. Consume the token with the outcome. An obsolete success or error changes no replacement state.

**Keep one current wait per activation.** Clear, cancellation and replacement close the old wait. An approved request with a bound `VcsRef` retains its original open wait until a newly requested head check completes the gate or fails it. An approval without a VCS binding completes through its saved contract and exact current-wait checks. A historical `approved` row cannot authorize a later activation. A head read started before approval cannot satisfy the final check. Responses and relevant push observations fence earlier reads, even when the push makes no logical state change. Poll scheduling never extends the wait's expiry.

**Use the existing scheduler with bounded cooperative providers.** One async dispatcher runs inside the MVP scheduler, including standalone. Due work and pending deliveries are discovered from durable rows independently of touched-run filtering. Finite invocation, integration-resource and namespace limits apply before creating tasks; nested requests are serial and use their declared resources. Timeouts stop result acceptance and initiate cancellation without stopping the scheduler loop. Credits remain occupied until the task settles. Integration retries do not consume agent attempts, and an unavailable head never means approval success.

Installed providers must keep import, initialization, synchronous work and hooks bounded, use nonblocking I/O and propagate cancellation. This supported fault model covers slow services and conforming provider failures. It does not contain arbitrary blocking or cancellation-resistant Python; standalone has the same limitation. Conformance exercises this contract without proving arbitrary installed code safe. The [dated failure-model evidence](../../knowledge/research/cooperative-provider-failure-model.md) explains the boundary. No new service, broker, worker authority or runtime call is introduced.

**Preserve immutable approval evidence and make push ordering explicit.** Changed-head observations for a pending request retain ADR-0008 invalidation. Ingress processing freezes the exact locally selected wait/request recipients before asynchronous handling; queued events do not attach themselves to replacement waits. Unordered generic ref pushes are conservative negative evidence for those recipients. A remotely old push first arriving during a newer request can therefore cause a conservative failure. Local tokens or arbitrary timestamps do not establish remote chronology. Equal-head pushes never authorize. A final equal-head sample is a point-in-time check; downstream consumes the approved immutable commit, and later external effects require their own provider conditions.

**Recover channel effects through existing approval rows.** Commit request/notification/closure intent before external dispatch. Only a current pending request desires an open channel interaction; approved, rejected, expired, invalidated, cancelled and superseded requests desire it closed. Closing the channel changes no approval audit decision. Approved gates awaiting verification remain unresolved locally, with their channel interaction already closed. A `resolved` notification links the canonical status page for approval/rejection; later gate failure remains visible there.

Terminal intent prevents new create authorizations and retries. A previously authorized call may still send or finish after cancellation; a database comparison cannot fence that remote send. Retain every learned external ID on the original request and close it, without restoring approval authority. Reconcile uncertain creation through read-only, paginated marker lookup; never create a new request merely to find a terminal one. Search-before-create is not generic exactly-once delivery. Notifications may repeat or arrive out of order; the API remains authoritative.

Once creation is forbidden, cleanup retains only compact identity, original resource, learned IDs and delivery progress. It holds no ReviewBundle/form payload, physical reservation or target hold. Uncertain delivery stays visible and is reconciled with bounded calls; absence alone proves no closure. Compact uncertainty may persist indefinitely, and MVP adds no abandonment action. Original resource identity is never redirected by an alias change. Current secret, namespace and network authorization still apply.

## Alternatives considered

- **Await providers inside pass transactions.** Rejected: external latency controls lock duration.
- **Keep only in-memory tasks.** Rejected: restart loses due work and cannot distinguish old answers from current authority.
- **Supervised child processes.** Deferred: stronger containment needs bounded IPC, termination/reaping and restart ownership before replacing children. It does not retract an accepted remote write.
- **A generic job/outbox service or observation jobs on runtime workers.** Rejected for MVP: existing owners hold the necessary intent; worker execution would require new claim/report authority and placement contracts.
- **Treat every push as a hint.** Rejected here: a move away and back could erase ADR-0008's conservative changed-push invalidation during pending review.

## Consequences

Cancellation and retry use short database decisions while a conforming provider is slow. Late answers cannot resolve new waits. Integration saturation, last observation, structured failures, retry times and pending delivery are visible through existing API, CLI, MCP and console reads. Credential repairs under an unchanged reference are discoverable through capped authorized probes; failed nodes still need ordinary manual retry.

This records a design, not an implementation or runtime test result. PostgreSQL/SQLite race contracts, operation defaults, error policy and provider conformance gates are specified below. Matching/transition ownership and deadline precedence remain separate contracts.

## Where it is applied

- [Data model section 14](../../knowledge/specs/data-model.md#14-external-observation-and-delivery-ownership-adr-0021): identities, lifecycles and future acceptance contracts
- [Scheduler section 16](../../knowledge/specs/scheduler.md#16-external-observation-dispatch-adr-0021): phases, locks, limits, errors and recovery
- [Protocol common rules and approval channel](../../knowledge/specs/protocols.md): cooperative calls, lookup, closure and conformance
- [API and agent surface](../../knowledge/specs/api-and-agent-surface.md), [console](../console.md), [architecture reference](../../knowledge/architecture/detailed.md)
- [NFR-1.2, NFR-2.5, NFR-5.1 and NFR-7.3](../requirements/non-functional.md)
- [Design review](../../knowledge/reviews/external-observation-design-review-2026-10-17.md)
