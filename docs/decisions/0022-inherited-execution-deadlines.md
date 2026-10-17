# ADR-0022: Inherit execution deadlines across managed lifecycle phases

- Status: direction and implementation plan accepted, 2026-10-17; document pending owner review
- Reviewed: implementation plan by the project owner, 2026-10-17; document wording pending review
- Deciders: project owner
- Absorbs: issue #57
- Supersedes in part: [ADR-0003](0003-scheduler-and-workers-over-http.md) and [ADR-0010](0010-cost-accounting-and-limits.md) (node-only local deadline), [ADR-0007](0007-attempt-model-and-retries.md), [ADR-0016](0016-trusted-execution-and-publication.md) and [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md) (publication recovery subject to expiry)

## Context

A remote runner that knows only a two-hour node deadline can continue after its enclosing run or subgraph expires while the control plane is unreachable. The canonical graph already combines a six-hour subgraph, three iterations and a two-hour child; a late iteration can encounter this gap. Recovery barriers prevent overlapping replacement, but do not stop unwanted computation. Calling every scope timeout hard without propagating the enclosing deadline overstates the guarantee.

The existing lifecycle includes preparation before submission, candidate execution, inspection and late publication on the runtime or worker host. A deadline carried only in `Workload` misses pre-submit preparation. A timer cannot retract a remotely accepted request, and elapsed time is not termination evidence.

## Decision

**Use one inherited immutable deadline.** At the first executing claim, persist the minimum of the configured node-attempt deadline, run deadline and every enclosing concrete subgraph-instance deadline. An absent optional limit contributes nothing; no applicable limit means no overall execution deadline. Record the determining scope identity and saved clock uncertainty with the deadline, and include them in the immutable execution request before any allocation or preparation effect. Ties attribute to the run, then the outermost subgraph, then the attempt. A graph definition name is not a concrete scope identity.

**Define clock starts once.** The run starts at its existing promotion from `pending` to `running`, excluding the accepted backlog. A subgraph instance starts on its first `ready` activation before its first body is materialized. A physical attempt starts on its first executing claim, before request sealing/preparation, excluding the earlier unclaimed queue. Adoption, worker restart, retry backoff and clear never reset an existing clock. A new physical attempt gets a new node clock; a subgraph's own iterations retain its original clock. A nested wrapper newly materialized in a new outer iteration gets its own start but inherits all existing ancestors. A new run gets a fresh run clock.

**Expired persistent scopes cannot be retried away.** A parent timeout closes that scope and cancels its descendants; a child's `retry_on: timeout` cannot escape it. Manual retry checks the run and ancestor wrappers, plus the target wrapper's own deadline when retrying a subgraph. Refuse an expired persistent scope before acceptance-quota acquisition or clear mutations with existing `API002`, reason `deadline_expired`, scope/deadline and rerun guidance. An old physical attempt's expired node deadline does not prohibit an otherwise eligible new attempt. Ordinary recovery barriers still constrain a rerun.

**Cover all managed phases.** Carry the saved binding through the existing authenticated protocol context, runtime registry/checkpoint and common executor, including `worker_host` preparation before `submit`. Guard first sealing, allocation, preparation, candidate launch, sealing/export, inspection, credential acquisition, finalization and publication/change-request authorization. The responsible runtime adapter/common executor locally cancels its managed phase without API connectivity and clips individual operation/plan-step bounds to the remaining deadline. The session runner remains the sole enforcer of harness event-count, soft-budget and stall limits; phase deadline enforcement also belongs at the host executor when a phase runs there.

**Use fixed nominal interruption and escalation thresholds.** Let the persisted UTC deadline be `D`. Interruption is due at `D` and forced-stop escalation at `D + 30s`, preserving the existing grace after timeout. Grace authorizes stopping, not another useful phase. Both thresholds derive from the original deadline, not delayed delivery or adoption time. An overdue escalation runs immediately; an earlier cancellation keeps its earlier stop schedule. Persist observed expiry as a sticky closed execution gate; a clock correction, new lease or configuration change cannot reopen it.

**State the clock and failure assumptions honestly.** A supported installation states a finite nonnegative `clock_uncertainty`, covering UTC error against the authority, conversion sampling and elapsed-time underestimation over its supported horizon. Save that assumption with the attempt. At a paired local UTC/monotonic sample `(w0, m0)`, conservatively schedule interruption at `m0 + max(0, D - w0 - clock_uncertainty)` and escalation at `m0 + max(0, D + 30s - w0 - clock_uncertainty)`. Later sampling may shorten an installed timer, never lengthen it. Restart reconstructs local timers from the unchanged UTC binding and closed state, not a fresh duration or a persisted cross-host monotonic value.

These are nominal action thresholds, subject to local scheduling and runtime-control latency. Neither stop-request dispatch nor physical termination is promised by an exact timestamp. An operator is not required to supply a fictitious worst-case scheduling bound. The partition guarantee assumes a runnable trusted supervisor, functioning local runtime control and the stated clock model. Host suspension, stopped supervisor, inaccessible runtime control and unsupported clocks do not gain automatic termination guarantees; overdue actions run when control returns, and ADR-0017 uncertainty persists until evidence arrives. The dated [clock evidence](../../knowledge/research/execution-deadline-clock-model.md) explains these limits.

**Guard result authority independently of timeout-pass order.** Existing API/scheduler transitions under the run lock compare the applicable deadlines and sticky expiry before granting new execute/publication authority, promoting current output or accepting success, including wrapper/wait completion. Exact equality is expired. A delayed timeout pass cannot accept late current results. Existing authenticated read-only replays of a sealed request or completed operation return their saved result without effect rights. A result accepted before expiry is not rolled back. Stall extension for a long tool call is capped by the inherited deadline.

**Keep remote uncertainty distinct.** Locally observed expiry prevents new authorizations and suppresses unsent work. A call authorized before expiry may pause before sending and may take effect afterward; there is no atomic remote send fence. A candidate may still execute during grace, and arbitrary candidate/MCP effects are not mediated by this deadline. Unfinished publication may be observed/reconciled after expiry, but authoritative remote absence does not authorize another push or change request. A prepared checkpoint cannot renew execution or publication authority.

**Allow bounded recovery, never renewed useful work.** Historical logs, diagnostics, partial usage, observation, retirement, dispatcher closure, reconciliation and storage cleanup may continue after expiry. Each operation has a finite configured provider/transport timeout and bounded output/batches, yields at its bound and cannot delay stop escalation. Required unresolved intent stays durable and may need later recovery claims indefinitely; there is no overall recovery lifetime or new recovery service. No cleanup path may launch candidates, produce new commits/pushes/change requests, promote current output or restore success.

**Preserve evidence and runtime trust boundaries.** Timeout records cancel intent and revokes result authority; terminal attempts, capacity and acquired holds still require affirmative execution/dispatcher/publication closure under ADR-0017/ADR-0020. Neither `D` nor `D + 30s` is a receipt. Local remains ordinary trusted Linux/macOS development/test execution; Docker and independent runtimes prove their declared domain termination separately. No new watchdog service, renewable lease, heartbeat-dependent cancellation, public operation, provider protocol or IR authoring field is introduced.

## Alternatives considered

- **Enclosing timeout as scheduler cancellation intent only.** Coherent but rejected: a disconnected runner could spend hours past a known parent deadline despite already implementing local node timeout.
- **Only replace the node deadline with a minimum in `Workload`.** Insufficient: host preparation precedes submission and publication may run on the worker host.
- **Hard real-time termination, stopped remote effects or released capacity by a timestamp.** Rejected: ordinary scheduling and external-call uncertainty do not establish those guarantees.
- **Renewable execution leases or another watchdog service.** Deferred as in ADR-0017; this change propagates fixed information through existing lifecycle controls.
- **A fresh grace on reconnect or finishing prepared publication after expiry.** Rejected: either extends the original authority and defeats the enclosing bound.

## Consequences

Run/subgraph limits now constrain disconnected managed work through a common local policy. Preparation consumes the node's duration; accepted backlog and pre-claim queue do not, while active parent clocks continue. Manual retry after an expired persistent scope requires a new run. Clock assumptions and nominal grace are explicit instead of implying instantaneous termination.

The existing attempt/claim/read projections carry the binding and observed expiry. CLI, MCP and console consume those reads; no operation or console action is added. Runtime implementation and real process evidence belong to the first execution slice. This documentation change specifies future release checks and runs only the repository's actual documentation/schema CI.

## Where it is applied

- [IR limits](../../knowledge/specs/ir.md), [data model section 15](../../knowledge/specs/data-model.md#15-inherited-execution-deadlines-adr-0022) and [scheduler section 17](../../knowledge/specs/scheduler.md#17-inherited-deadline-enforcement-adr-0022)
- [Runtime lifecycle and conformance](../../knowledge/specs/protocols.md), [API and agent surface](../../knowledge/specs/api-and-agent-surface.md), [architecture](../architecture.md) and its [internals](../../knowledge/architecture/internals.md)
- FR-6.5, FR-6.8 and FR-9.4 in [functional requirements](../requirements/functional.md); NFR-8.2 in [non-functional requirements](../requirements/non-functional.md)
- [Design/review evidence](../../knowledge/reviews/inherited-execution-deadline-review-2026-10-17.md)
