# ADR-0010: Cost is a token ledger with a pluggable price table; USD limits are soft, hard limits are timeouts and event counts

- Status: accepted, 2026-10-15; amended 2026-10-16 (run and sub-graph budgets enforced by the scheduler on aggregate usage; [architecture critique of 2026-10-16, finding A11](../../knowledge/reviews/architecture-critique-2026-10-16.md#a11-aggregate-budget), issue #13)
- Reviewed: by the project owner, 2026-10-16
- Amended by: [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md), 2026-10-17 (observation-gap completeness); wording pending owner review
- Deciders: project owner, after the design review of 2026-10-15 (first cycle, production-readiness lens; second cycle, simplicity pass), see [review-2026-10-15.md](../../knowledge/reviews/review-2026-10-15.md)
- Absorbs: DD-31, the cost-model requirements in FR section 9 and NFR section 8, the usage-completeness part of requirements decision 11, the `reasoning_tokens` item of DD-37
- Related: [ADR-0004](0004-harness-adapters-over-vendor-sdks.md), [ADR-0007](0007-attempt-model-and-retries.md), [ADR-0008](0008-content-bound-approvals.md), [ADR-0011](0011-namespaces-and-personal-tokens.md), [ADR-0012](0012-no-transform-node.md)

## Context

A run of a kora graph can hold many agent sessions, retries and fan-out losers, each consuming model tokens on an organisation's account. The platform team needs to know what a run cost, stop runaway spend, and later derive cost per accepted change (NFR-8.3). The harnesses make this hard: Claude Code reports per-model usage with a cost basis on its final result; Codex reports usage only when a turn completes, so a killed turn is unaccounted; a subscription session has no USD price at all. Prices and gateway contracts differ per organisation and change over time. The production-readiness lens of the design review of 2026-10-15 (first cycle) found no spend guard above the per-run budget and no way to attribute a mid-run price change to the attempts it affected.

## Decision

- **Unit of record: tokens per model per attempt.** One `usage_record` per `(attempt, model)` with `input_tokens`, `cached_input_tokens`, `cache_creation_tokens`, `output_tokens` and `reasoning_tokens`, read from the harness result (Claude Code `usage`/`modelUsage`, the Codex app-server's turn-completion usage report). Each record carries `completeness`: `complete` when the final result event was parsed, `partial` when summed from streamed per-turn events after a kill, crash or `max_events` termination, `unavailable` when nothing was reported. For `resume` retries the record stores the delta of whole-session totals (`delta_of_session_totals`). Run and node totals are sums over rows, never stored, and carry the weakest completeness of their parts.
- **Price table as a pluggable source.** `usd_estimate` is derived at write time from `basis`: the harness's own figure when the basis is `harness_list` or `harness_managed`, else the active `price_table` row (a shipped default of list prices, overridable per namespace for a contract or gateway, with `effective_from`). `reasoning_tokens` are a column of their own because Codex reports them and they are priced separately (DD-37). Each attempt snapshots `price_table_id`, so a table changed mid-run stays attributable. Every record carries `billing_class` (`api_key`, `subscription`, `gateway`, `managed`); subscription runs are labelled "not billable in USD". All USD figures are labelled as estimates with their basis.
- **USD limits are soft.** `budget_usd` exists at agent node, sub-graph and run level. The agent node's budget is enforced by the session runner inside the runtime from streamed usage: it tolerates the overshoot in `runtime.budget_headroom_pct`, then terminates the session (interrupt, 30 s grace, kill) with error class `budget_exhausted`. Native budgets are set in addition where they exist (Claude Code `max_budget_usd`). The sub-graph and run budgets are enforced by the scheduler, because no single session can see the others: it sums the usage records of finished attempts and the live counters of running ones per scope and admits no further attempt in a scope over its budget plus headroom; a ready node in that scope fails with the same error class, while running attempts finish and keep their outputs, the rule the daily namespace budget already follows. Sub-graph budgets span all sub-graph attempts.
- **Hard limits are only timeouts and event counts.** `timeout` at node, sub-graph and run level is enforced by the scheduler on the wall clock, and the node deadline also travels to the session runner, which stops the session at it even when the control plane is unreachable; `max_events` counts turns, tool calls and tool results in the SDK event stream and is enforced by the session runner (`events_exhausted`); `stall_after` is likewise hard. None depends on price data or harness-reported usage.
- **Daily soft cost limit per namespace** (DD-31). One policy field, `daily_budget_usd`. When the namespace's estimated spend for the current day (sum of `usage_record.usd_estimate` over attempts started that day, in the namespace's timezone) exceeds it, the admission pass admits no new attempts and running ones finish. The state is exposed in `/metrics` (`kora_namespace_daily_spend_usd`, `kora_namespace_budget_blocked`) and on `GET /namespaces/{ns}/cost/daily`.
- **Cost on approval requests** is the plain sum of the run's usage records at request time, attached by kora to the `approval_request` row (`run_cost`, per model, with the weakest completeness); no lineage walk, no agent computes it.

## Alternatives considered

- **Hard USD kill switches.** Rejected: a USD figure is an estimate that depends on a price table, a billing class and a harness that may report nothing for a killed turn; a hard stop would turn accounting gaps into failed runs.
- **No cost accounting, relying on vendor dashboards.** Rejected: dashboards cannot attribute spend to a run, node, attempt or fan-out instance, and cannot feed an admission decision or an approval request. Reconciliation against vendor usage APIs is post-MVP (FR-9.6).
- **A fixed built-in price table.** Rejected: list prices change and organisations pay contract or gateway prices; the shipped table is only the default.
- **Walking artifact lineage for the cost shown on an approval.** Dropped as a special case by the simplicity lens of the design review of 2026-10-15 (first cycle); the plain run sum replaced it.

## Consequences

- Cost per accepted change, including failed attempts, retries and fan-out losers, is derivable offline from `artifact_lineage` and `usage_record` (NFR-8.3); usage records are retained forever by default.
- Run and node views (CLI, API, the `kora_cost` MCP tool) show tokens, estimated cost and completeness per attempt; fan-out views aggregate across instances (FR-9.5).
- A session can overshoot a soft budget by the headroom plus whatever the harness spends before the stream reports it; with healthy reporting a run or sub-graph can overshoot by that amount per running session plus one heartbeat interval. During a partition, reporting staleness is unbounded and displayed spend is last-known/partial with its observation time (ADR-0017); the documentation must say so (NFR-8.2).
- The cost formula and the limit arithmetic are among the few places where unit tests are warranted in this project.

## Where it is applied

- [FR-6.6](../requirements/functional.md), [FR-6.9](../requirements/functional.md), [FR-9.1–9.6](../requirements/functional.md)
- [NFR-3.2](../requirements/non-functional.md), [NFR-8.1–8.3](../requirements/non-functional.md)
- [ir.md §3](../../knowledge/specs/ir.md) (`AgentSession` payload, cost attached to approvals), [ir.md §4.2](../../knowledge/specs/ir.md), [ir.md §5](../../knowledge/specs/ir.md) (sub-graph budgets), [ir.md §9](../../knowledge/specs/ir.md) (limits table)
- [data-model.md §2](../../knowledge/specs/data-model.md) (`usage_record`, `price_table`, `attempt.price_table_id`, `namespace.policy.daily_budget_usd`, `approval_request.run_cost`), [data-model.md §5](../../knowledge/specs/data-model.md), [data-model.md §7](../../knowledge/specs/data-model.md) (completeness)
- [scheduler.md §6](../../knowledge/specs/scheduler.md) (admission and the daily limit), [scheduler.md §11](../../knowledge/specs/scheduler.md) (metrics)
- [protocols.md §3](../../knowledge/specs/protocols.md) (usage mapping), [protocols.md §14](../../knowledge/specs/protocols.md) (`limits.budget_usd`, usage source, billing class per harness)
- [api-and-agent-surface.md §2](../../knowledge/specs/api-and-agent-surface.md) (cost routes)
- [design-decisions-2026-10-15.md](../../knowledge/reviews/design-decisions-2026-10-15.md) DD-8, DD-31, DD-37
