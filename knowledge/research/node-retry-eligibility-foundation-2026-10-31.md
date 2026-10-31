# Pure node retry eligibility foundation

Date: 2026-10-31. Implementing issue: [#559](https://github.com/realkarych/kora/issues/559). Accepted baseline: `d54e1946d48d92159ff262c438bf57fd2d19db49`.

## Contract and delivered boundary

[Scheduler section 7](../specs/scheduler.md#7-retry-handling), [IR section 6](../specs/ir.md#6-retry-policy-and-strategy-fr-63-fr-64) and [evaluation errors](../specs/ir.md#77-evaluation-errors) govern this private policy gate. ADR-0007/0017/0018/0022/0024 retain history, recovery, saved-policy and deadline authority. First-slice refusal remains active; this foundation activates no automatic retry.

`kora.engine.retry_models` holds frozen complete projected facts and closed results. `kora.engine.retry.decide_node_retry_eligibility` returns only node-policy denial, predicate-required, node-policy allowance or delegation to a separate runtime retry policy. The engine initializer exposes no new entry point. There is no caller, database query/write, counter update, wait, event, backoff, clock, new attempt, strategy selection or resource release.

The caller supplies the original failure class and consumed policy-attempt count, including the failed ordinary attempt. This is not physical attempt.number; infrastructure/provider failures do not consume it. No producer, increment, reset, subgraph iteration or storage mapping is selected. Policy maximum and memberships are explicit supplied saved values, never current defaults or inferred omission. Predicate configuration distinguishes no predicate from an evaluation not yet performed.

The four never-retry classes remain denied even if selected. Deny membership wins over allow membership, then exhausted count denies. Only an otherwise eligible configured predicate can request evaluation or affect this decision. Exact true permits policy eligibility; false denies. Every existing CEL ErrorKind denies while retaining the original failure class. An earlier class/list/count denial acquires no supplied predicate error as its cause.

Infrastructure/provider_limited route to their own policy owner without ordinary allowance, predicate use or count effects. This delegation approves neither the runtime policy nor replacement. An exact closed membership projection is not a complete authoring/deployment validator: the accepted prohibition on runtime classes in ordinary retry_on remains with authoring/loading, regardless of the defensive routing result.

## Checked facts and authority limits

Constructors require exact kinds, the existing maximum range, valid count domain, closed enum memberships and consistent predicate presence/state. Malformed or incomplete projected fields raise fixed content-free ValueError. These checks establish shape and consistency, not provenance, saved-contract compatibility, currentness, temporal freshness or whole-policy validity. No raw IR/policy parser or persisted format is introduced.

A positive result is not an execution, admission, replacement or release token. A future producer must load the complete saved contract and apply the existing missing-policy compatibility refusal. A future authoritative application must recheck installation support, current identity, cancellation, closure/recovery, fresh all-ancestor deadlines, capacity, input availability and validated outputs under the existing lock. Existing closure obligations cannot be delayed by missing policy or a predicate request.

Pending #545 cancellation/outcome/deadline representations and #555 failure-reader decisions remain separate. Server work items 9/10 own future retry application and counted-fact production; deployment/CEL owners retain policy normalization, authoring validity and real expression projection/evaluation. #216 retains input/feedback availability. Runtime #195–#199 and delivered completion/AuthContext are unchanged.

## Content, ownership and verification limits

The gate retains only bounded closed policy values, count and predicate result/kind. The evaluator keeps the complete original EvaluationError record, including field/time/fault, for its future failure event. No expression, artifact payload, native exception, diagnostic or content-bearing error record enters this projection. Pure helpers own no process, connection, timer, cache, buffer or sink. Future authoritative callers retain their observability activation and safe reporting obligations.

The issue records actual independent correctness/security design and source results, commands and final-revision CI provenance. Pure state tests cover class/list/count/predicate precedence, original-class retention, separate runtime routing, complete fixed refusal and frozen values. They cannot prove saved-policy/count production, output projection, error-event persistence, locked transitions, replay, first-slice execution refusal, both-database retry integration or resource release. Existing integration gates remain pending their real owners.

No new numerical acceptance method or timing/resource guarantee is introduced. Exact enum membership bounds the supplied sets; no graph scan or cache is added. The prior available master mutation report covers pricing/CEL rather than an implemented retry gate and adds no PR campaign or stale-report hold.
