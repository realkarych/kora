# ADR-0003: Scheduler and workers are separate processes; workers reach the control plane only through the HTTP API

- Status: accepted, 2026-10-15; amended 2026-10-16 (reclaim on lease expiry alone, adoption within the execution domain; [architecture critique of 2026-10-16, finding A07](../../knowledge/reviews/architecture-critique-2026-10-16.md#a07-execution-domain), issue #13)
- Reviewed: by the project owner, 2026-10-16
- Amended by: [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md), 2026-10-17 (execution uncertainty, dispatch closure and retained reservations); wording pending owner review
- Amended by: [ADR-0022](0022-inherited-execution-deadlines.md), 2026-10-17 (inherited lifecycle deadlines and expiry); wording pending owner review
- Amendment: durable readiness discovery, 2026-10-17, issue #70; direction, implementation plan and final wording accepted by the project owner on 2026-10-17. Evidence: [discovery review](../../knowledge/reviews/durable-readiness-discovery-review-2026-10-17.md).
- Deciders: project owner, after the design review of 2026-10-15 (both cycles)
- Absorbs: DD-13, DD-15, requirements decision 18 (the worker part), DD-36 (claim response, lease and polling defaults)
- Related: ADR-0001, ADR-0004, ADR-0007, ADR-0011, ADR-0013
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

## Context

A kora run lasts hours to days and may hold hundreds of concurrent agent sessions (NFR-1.1). Sessions execute in runtimes that can be far from the control plane, including an adopter's enterprise runtime (an in-house CI and execution platform) inside a closed network. The MVP acceptance test requires the canonical graph to run unchanged there, so whatever drives the runtime must deploy inside the adopter's network with minimal privileges.

The first drafts let workers write to the metadata database directly as well as through the API. The design review of 2026-10-15 ([record](../../knowledge/reviews/review-2026-10-15.md)) flagged two paths for one job, leases without fencing that let a worker write after losing its lease, and stall detection that fired during long tool calls.

## Decision

**Three processes** (DD-13). `kora api` (HTTP API, webhooks, worker and MCP endpoints), `kora scheduler` (the passes: triggers, readiness, admission, waits, timeouts, completion, clear-downstream) and `kora worker`, one per runtime name. The scheduler decides and persists; a worker claims attempts, prepares the workspace, submits to the runtime, tails the session's normalised event stream, relays the runner's counters, uploads segments and artifacts, and finalises. Workers never decide and never enforce limits: the session runner inside the runtime is the sole enforcer of `max_events`, the soft budget and `stall_after`.

**Inherited lifecycle deadlines** ([ADR-0022](0022-inherited-execution-deadlines.md)). The first executing claim fixes the earliest node, enclosing-subgraph and run deadline before preparation. Runtime/common executors enforce it in their own placements, including pre-submit worker-host preparation and late publication; this does not move harness event/budget/stall enforcement to the worker. Expiry has fixed nominal interruption and 30-second escalation thresholds, never renewed on adoption, and does not prove termination or release reservations.

**One path to the control plane.** A worker talks to the HTTP API and only to the API, outbound, with a service token bound to a runtime and a namespace list (FR-8.9). There is no direct-SQL path and no database credential on a worker. `kora standalone` runs api, scheduler and a local worker as coroutines in one process on SQLite; its worker uses the same API client over an in-process ASGI transport, so nothing exists only in standalone mode (NFR-0.1).

**Durable scheduling discovery.** Each run has one durable boolean `needs_scheduling`. Every graph-relevant transition sets it in the same transaction, under the run lock; creation initializes it true. Ordinary discovery queries marked runs without a timestamp cutoff and revisits them in finite rotating batches. `updated_at` and notifications are diagnostic/latency aids, not correctness cursors. Under the same run lock, the scheduler clears the marker only after a fresh check that no local pass can currently progress; checking and acknowledgement share a transaction. Work remaining at the ten-round cap retains the marker. Restart uses the same durable predicate.

Timed waits/admission rechecks, pending promotion, observations/delivery, deadlines and execution recovery remain independently discoverable, including terminal-run recovery. Applying their graph-relevant changes marks the run. An unresolved external call or future timer may coexist with a cleared marker only because its own durable discovery remains active. The marker does not authorize an effect or replace transactional clear-downstream/result authority; ADR-0007's rejection of a dirty set for invalidation is a different concern. Full discovery, local processing and worker claiming must meet NFR-3.1 at MVP scale without notifications, while queries remain incremental under NFR-3.3.

**Claim, lease and fencing** (defaults from DD-36):

- The scheduler inserts an `attempt` row in status `created`. A worker polls `POST /workers/{id}/claims {runtime, max: 1}` every 1 s and receives one self-contained claim or none: attempt, lease `{epoch, until}`, run parameters, node config, limits, inputs and outputs, secret aliases.
- Every claim increments `lease_epoch`. Every heartbeat, upload and transition carries it; the API rejects a stale epoch with `API008`, and a worker whose renewal is rejected stops relaying, closes its dispatch gate, settles in-flight operations and records a monotonic closure acknowledgement in the original runtime registry. The epoch travels in API calls only, never inside the `Workload` handed to the runtime.
- The lease is 120 s, renewed every 15 s by `POST /attempts/{att}/heartbeat {lease_epoch, counters}`, whose response carries `desired_state` and `lease_until`; cancellation reaches the worker through it. Heartbeats also refresh `worker.last_seen_at`; there is no separate worker heartbeat route.
- An expired lease makes an attempt reclaimable for recovery in its original, non-reused execution domain. It sets `execution_state=unknown`, never terminal `lost` by time alone (ADR-0017). The API records every dispatcher incarnation and lease grant before returning authority. A fresh worker may observe/adopt the same live attempt; earlier dispatchers must close their gates and settle in-flight operations before platform writes resume. Cancelled/superseded attempts remain recoverable without submission or publication rights, including after run cancellation. Missing or unreachable registry state cannot prove absence; replacement requires affirmative runtime termination and closure of every dispatcher and platform publication operation.
- Cancellation and clear-downstream revoke the attempt's result authority in the same transaction that orders the cancel ([ADR-0007](0007-attempt-model-and-retries.md)): its later uploads are historical, never current.

**Stall detection** (DD-15). Agent-loop liveness is `last_event_at`, advanced by every normalised event. After `stall_after` of silence the session runner terminates the session; the attempt fails with class `stalled`, which the node's retry policy may retry. `stall_after` resolves from the node's `limits.stall_after`, else the graph's `runtime.stall_after`, else the runtime's `default_stall_after` (15 minutes in the reference runtimes). The window is extended while the last event is an unanswered tool call, up to the inherited execution deadline, so a long dev-run inside one `Bash` call is not a stall.

**Waits hold no worker** (NFR-1.3). A waiting node is one `wait` row (plus an `approval_request` row for approvals): no coroutine, no container, no lease. Resolution by time, event or poll happens in scheduler passes; a sensor poll is a short call.

## Alternatives considered

- **Workers with direct database access.** Rejected: an adopter's worker in a closed network would need database credentials and an inbound route, and two write paths double the fencing surface.
- **A single process.** Rejected for the platform: the scheduler must be stateless and replaceable (NFR-1.2) and workers must scale per runtime and sit in other networks. It survives only as `kora standalone`.
- **Timestamp or sequence watermarks as the sole discovery mechanism.** Rejected: a transaction may commit after a scan with a timestamp or allocated sequence behind its cursor. A notification is not durable continuation.
- **Recurring sweeps of all active runs.** Not selected: a durable marker avoids repeated evaluation of unchanged waiting runs; its complete transactional marking and acknowledgement obligations are explicit in the specifications.
- **Leases without an epoch.** Rejected: a worker that lost its lease could still write.

## Consequences

- Enterprise runtimes need one outbound HTTPS route, the placement the acceptance test requires.
- Cancellation is delivered at heartbeat granularity (15 s); the attempt's right to publish a current result ends at the cancel's commit, not at its delivery.
- A lost `local` or `docker` host blocks replacement until termination and all prior dispatch/publication authority are established as closed. Only portable runtimes adopt a live handle across hosts; a stable worker name alone cannot prove old-domain absence.
- Admission latency includes complete candidate discovery, bounded local processing and the 1 s worker claim interval, under 5 s at MVP scale even without notifications (NFR-3.1).

## Where it is applied

- [scheduler.md §1](../../knowledge/specs/scheduler.md) (processes), [scheduler.md §3](../../knowledge/specs/scheduler.md) (claims, leases, adoption), [scheduler.md §4](../../knowledge/specs/scheduler.md) (stall detection), [scheduler.md §5](../../knowledge/specs/scheduler.md) (waits), [scheduler.md §10](../../knowledge/specs/scheduler.md) (standalone)
- [data-model.md §1](../../knowledge/specs/data-model.md) (one writer path) and the `attempt`, `wait` and `worker` tables in [data-model.md §2](../../knowledge/specs/data-model.md)
- [protocols.md §2](../../knowledge/specs/protocols.md) (`RuntimeCapabilities.default_stall_after`, the session runner)
- [api-and-agent-surface.md §2](../../knowledge/specs/api-and-agent-surface.md) (worker and attempt-reporting routes)
- [ir.md §2 and §9](../../knowledge/specs/ir.md) (`runtime.stall_after`, `limits.stall_after`)
- [component map](../technical-introduction.md#3-the-component-map), [deployment shapes](../../knowledge/architecture/detailed.md#deployment-shapes) and [stack](../../knowledge/architecture/detailed.md#stack-per-component)
- [FR-6.1, FR-6.1a, FR-8.9](../requirements/functional.md); [NFR-1.2, NFR-1.3, NFR-2.1, NFR-2.3](../requirements/non-functional.md)
