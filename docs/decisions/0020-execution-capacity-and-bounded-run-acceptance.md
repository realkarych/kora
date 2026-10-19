# ADR-0020: Separate bounded run acceptance, execution capacity and target ownership

- Status: direction and implementation plan accepted, 2026-10-17; document wording pending owner review
- Reviewed: implementation plan by the project owner, 2026-10-17; document wording pending line-by-line review
- Deciders: project owner
- Resolves: issue #55
- Amends: [ADR-0014](0014-target-identity.md) (deferred-run occupancy and independent progress), [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md) (overlap before materialisation)
- Related: ADR-0002, ADR-0003, ADR-0007, ADR-0017, ADR-0018, ADR-0019
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.
- Amended by: [ADR-0002](0002-ai-first-api-and-thin-clients.md), 2026-10-19 (start outcomes are immediate tool results; [issue #124](https://github.com/realkarych/kora/issues/124)); wording pending owner review

## Context

Counting every `running` run toward `max_running_runs` makes a target waiter consume execution capacity. At a graph limit of two, a Q holder waiting for review and a deferred Q run fill both slots, blocking eligible R work. ADR-0014 accepted this consequence while FR-6.11 promised that target deferral would not hold back another target. Exempting only a `target_held` wait would leave approvals, timers and retry waits with the same problem and make accounting depend on a periodically recreated diagnostic row.

A bounded execution queue also needs bounded acceptance. A limit on only idle runs can overflow when active runs enter review. A limit on only run rows can move the backlog into acknowledged webhook events waiting to become runs.

## Decision

**Count physical execution independently of run lifecycle.** A graph's `max_running_runs` counts distinct run IDs having any non-terminal physical attempt, across all versions of that graph. Include `created` through `finishing`, historical and superseded attempts, cancelled runs and unknown execution. Another attempt in an already counted run adds no run slot, while every attempt retains its ordinary physical attempt/node/runtime reservations. Fan-out max_parallel preserves its body-instance unit through waits; parallel nodes within one body add no second body unit, and old unresolved body work retains that logical reservation. The first attempt insert acquires the run slot; the last terminal transition releases it. ADR-0017's termination and dispatcher/publication closure remain required. No lease expiry, logical cancellation or wait row releases a physical reservation.

Approval, sensor, timer, retry-backoff, target and capacity waits with no physical attempt consume no execution-run slot. A waiting branch does not free the slot while another branch is active. Wrappers count through their physical body attempts, not their own `running` label. A successful approval makes downstream work eligible; it promises no immediate execution. No run-admission state or reservation table is added.

**Bound outstanding accepted work separately.** `namespace.policy.concurrency.max_pending_runs` retains its name but counts each run once when it is `pending` or `running`, has a non-terminal attempt, or retains a target hold. It defaults to 1000 and must be a positive finite integer. It bounds accepted outstanding runs, not permanent history, artifact bytes or approval rows within a fan-out. Settled failure releases the count even though manual retry remains possible. Retrying a settled failed run acquires outstanding capacity before clearing anything; saturation refuses it without mutations. An already outstanding run needs no second credit. Reads, cancellation and recovery remain available at saturation.

**Accept each start through one transaction.** Manual, webhook, cron, sensor, graph-triggered and rerun starts use the shared start-run entry point. An authenticated new webhook validates its chosen version, deduplicates, applies overlap, checks outstanding capacity and persists a pinned `pending` run and compact delivery outcome before acknowledgement. A retained trigger event is a linked receipt, never an acknowledged unconverted queue. Wait-addressed events retain their existing wake path. After current authentication and envelope checks, an accepted replay compares the original request digest and returns its recorded run or skip outcome before current graph/schema/target validation or quota checks. Deployment, a full quota and response loss cannot turn one accepted delivery into another run.

Full outstanding capacity returns HTTP 409 / `API002`, `retryable: true`, reason `run_backlog_full`, with scope, limit and usage. It creates no run or pending trigger event and does not consume the key as an accepted outcome or cache an unrecoverable refusal. A later client retry may succeed. Cron, start-sensor and graph-triggered occurrences instead record explicit rejection and advance their occurrence cursor; they create no hidden retry queue or catch-up promise. Existing trigger reads expose the last outcome/reason/time and metrics count refusals. A rejected occurrence is visible, not silently dropped.

**Keep overlap a graph lifecycle rule.** Defaults remain unchanged. `allow` imposes no lifecycle exclusion. `skip` returns a durable, deduplicated no-run outcome when any accepted `pending` or `running` run of the graph exists, closing the pre-materialisation burst window. `queue` accepts a bounded pending run; promotion under the graph scope lock selects the oldest pending run only when no run is `running`. Every tick revisits pending promotion candidates independently of the run scheduling marker ([ADR-0003](0003-scheduler-and-workers-over-http.md)), so an untouched queued run wakes when its predecessor ends. Other explicitly allowed starts remain allowed; same-run manual retry retains its existing semantics. A graph that wants per-target concurrency uses `overlap: allow`. `skipped` is a start-operation outcome, not a new run status.

**Use one live graph-wide concurrency policy.** Counts span versions; the highest-numbered active graph version supplies `runtime.concurrency`, falling back to the highest-numbered deployed version when none is active. Deploy/retire and admission serialize under the graph scope lock. These are live admission caps, overriding an older run's IR concurrency declarations only for admission; execution topology, inputs, target declaration and attempt semantics remain pinned. Reads name the cap-source version. An absent graph cap adds no graph restriction; other scopes still apply. Lowering limits evicts nothing and blocks only positive additions until there is room; already counted runs may add attempts subject to all attempt limits.

**Retain target ownership independently.** Existing holds survive review and soak while the run remains `running`, even with no execution slot. Release still requires the ordinary run-release condition and ADR-0017 closure. An awakening holder competes for execution capacity without priority. Incoming `allow` can create several holders; an unheld `defer` candidate waits for every other holder; an already acquired hold is not reacquired. Binding, discovery and scheduler-only paths retain ADR-0019's boundaries: no retroactive protection, retargeting or universal approval-only hold.

**Serialize decisions, then scan past blocked work.** Run locks precede the ordered global, namespace, graph, runtime, fan-out and target locks; counts use fresh reads after locks. Inserts, terminal releases, hold changes and policy writes follow compatible locking on both databases. No other holder's run lock is acquired under a target lock. Reason priority can differ from lock order: expose recovery or target blockage before generic capacity when applicable, but acquire neither attempt nor hold until every check passes. The single-scheduler MVP visits candidates FIFO by stable `node_instance.id`, skips ineligible ones and continues. Recreated waits do not reset age. Concurrent participants preserve capacity safety, without strict multi-scheduler FIFO, starvation freedom or weighted fairness.

**Expose the three facts through existing surfaces.** Namespace/graph/run/node/target reads show effective scope, limit, usage, cap-source version and observation time; run execution occupancy and outstanding contribution; target holders and recovery blockers; recheck time and next action. Large blocker sets include an explicit count, bounded sample and existing paginated read reference. CLI and MCP forward the same API data; the console adds read-only information, keeping its four mutations. Existing kora_reference explicitly maps namespace policy/admission and graph-trigger reads with ordinary namespace authorization. `kora_trigger` returns a no-run skip, including a replayed one, as its immediate result, like every other start outcome (ADR-0002). No operation or curated tool is added.

## Alternatives considered

- **Keep unfinished-run occupancy.** Rejected: idle Q deliveries still block useful R execution.
- **Exempt only target-wait rows.** Rejected: other waits remain inconsistent, active parallel work can be overlooked and graph blockage can mask the target reason.
- **Persist explicit admitted-run membership.** Rejected for MVP: membership releasing at the last physical attempt duplicates an existing predicate and adds another recovery lifecycle.
- **Reserve an acceptance credit on an unconverted trigger event.** Valid, but requires ownership transfer, abandoned-event release and additional recovery rules. Canonical pending-run acceptance uses the existing run as the reservation.
- **Guarantee new-target acceptance despite arbitrary floods.** Deferred: a finite shared outstanding quota can fill. Per-target allocation, priorities and weighted fairness are separate policies.

## Consequences

A target-deferred run with no physical attempts neither occupies execution capacity nor stops the eligible-candidate scan. An accepted R can use spare capacity while Q waits. Genuine physical occupancy, a full shared outstanding quota or graph overlap can still delay/refuse R; FR-6.11 no longer promises unconditional acceptance or progress.

Releasing idle execution capacity applies to graphs without target declarations too. More accepted runs can reach human review; the outstanding-run quota bounds runs, not fan-out approval count. Settled failed retry may be refused until space returns. A planned internal start can be explicitly rejected at saturation; callers needing retry use the documented client behavior. Unknown execution can retain capacity indefinitely until closure, preserving ADR-0017's availability tradeoff.

The unreleased IR remains 1.0. This is a design change with future database/runtime acceptance contracts, not an implementation or a claim that integration tests passed. Provider quotas, budget-check ordering, external observation transactions and deadline enforcement remain separate issues.

## Where it is applied

- [FR-6.2, FR-6.8, FR-6.10–6.11](../requirements/functional.md), [NFR-1.3, NFR-7.4](../requirements/non-functional.md), [ADR-0014](0014-target-identity.md), [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md)
- [Data model section 13](../../knowledge/specs/data-model.md#13-run-acceptance-and-execution-capacity-adr-0020): exact predicates, state table, delivery outcomes and future tests
- [Scheduler](../../knowledge/specs/scheduler.md): start, promotion, admission, release, locks, retry, metrics and both databases
- [IR](../../knowledge/specs/ir.md), [IR schema](../../knowledge/specs/ir.schema.json): existing concurrency fields and live cap source
- [API and agent surface](../../knowledge/specs/api-and-agent-surface.md): counters, refusals, replay, diagnostics and client parity
- [Scheduler components](../../knowledge/architecture/detailed.md#components-inside-the-scheduler), [console pages](../../knowledge/design/console/pages.md), [console states](../../knowledge/design/console/states.md)
