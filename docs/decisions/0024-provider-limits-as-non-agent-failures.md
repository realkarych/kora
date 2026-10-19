# ADR-0024: Model-provider rate limits and quotas are a non-agent failure that waits for the reset

- Status: direction and implementation plan accepted, 2026-10-18; the acceptance was given by the project owner and relayed by a contributor; document wording pending owner review
- Deciders: project owner
- Resolves: issue #41
- Amends: [ADR-0004](0004-harness-adapters-over-vendor-sdks.md) (the adapter classifies provider limits), [ADR-0010](0010-cost-accounting-and-limits.md) (the provider pause sits with the other admission limits)
- Related: ADR-0007, ADR-0017, ADR-0020, ADR-0021, ADR-0022

## Context

kora has no notion of a model provider's rate limit or quota. A harness session that hits one ends as `harness_error`, which a node's retry policy normally retries within minutes, so all attempts are spent and the run fails. A quota that resets five hours later would have let it succeed. When a `fan_out` and other runs share one credential, every attempt hits the limit and retries on its own. After the reset they all wake together and hit it again.

The harnesses report limits explicitly. The sources below were read on 2026-10-18; no live session has confirmed them:

- the Claude Agent SDK types at `anthropics/claude-agent-sdk-python@f93e29cc951c` and its TypeScript reference;
- the Codex app-server JSON schemas at `openai/codex@7b44ac166143`.

| signal | Claude Agent SDK | Codex app-server |
|---|---|---|
| limit reached | `rate_limit_event` with `rate_limit_info.status = "rejected"`; assistant `error = "rate_limit"`; result `api_error_status = 429` | `error` notification whose `codexErrorInfo` is `usageLimitExceeded` or `rateLimitExceeded` |
| reset time | `rate_limit_info.resetsAt` and `rateLimitType` | `account/rateLimits/updated` windows with `resetsAt`, `usedPercent`, `limitId` |
| not a temporary limit | `authentication_failed`, `billing_error`, `account_on_hold`, `credits_required` | `unauthorized`, `badRequest`, credits-depleted `rateLimitReachedType` |
| transient capacity | `overloaded` (HTTP 529), `server_error` | `serverOverloaded`, `internalServerError`, connection failures |
| retrying internally | the `api_retry` system message; `tool_progress.subagent_retry` | `error` with `willRetry: true` |

Three things are not confirmed:

- whether reset fields are present for API-key accounts and not only for subscription plans;
- the exact fields of Claude's `api_retry` message;
- the ordering between the limit signal and the terminal result.

## Decision

**A new error class, `provider_limited`.** The harness adapter maps the vendor signal to a normalised event that carries:

- `retry_after`: the matching window's reset time, or null;
- `scope`: `rate` or `quota`;
- the limit group, the model and the harness binding.

The scheduler contains no vendor-specific code. Credits or billing exhaustion and account holds are `config_error`, never retried, because waiting does not help. Authentication failures stay `config_error`. Overload and server errors stay `harness_error` under the node's retry policy.

**It does not consume agent attempts.** As with `infrastructure` (NFR-2.4), a separate policy decides: `runtime.provider_retry`, with a namespace default. The policy is bounded by a persisted deadline, `max_wait` (default 6 hours, which covers a five-hour subscription window) after the node's first provider limit or its first admission refusal by a provider pause, whichever comes first, not by a count. The second case covers a node that has never executed and is held back by a limit another node observed on a shared quota. Every wake is capped at that deadline, quota-caused admission waits count towards it, and at the deadline the node fails with `provider_limited` even when the reset is days away.

**The wait holds nothing.** The node gets an ordinary `wait` row:

- with a reset time: `wake_at` is `retry_after` plus jitter;
- without one: exponential back-off from 1 minute, doubling to 30 minutes, with jitter.

Neither case holds a worker or a runtime slot.

**One pause per quota, not per alias.** Each harness adapter's deployment binding declares its `quota`: a **quota resource** naming the provider account or organisation whose limits apply, the **quota groups** with the model patterns each covers, and the mapping from vendor limit-group names to those groups, with defaults shipped by the adapter. The scheduler decides only on these declared ids and patterns. Under ADR-0021's rule for deployment bindings, aliases on one account declare the same quota resource; a binding without one is its own resource. The pause is keyed by `(quota resource, quota group)`:

- the quota group is the binding's mapping of the vendor group the signal names: Claude's `rateLimitType` (by default `seven_day_opus` and `seven_day_sonnet` map to model groups, `five_hour` and `seven_day` to an account-wide group), or Codex's `limitId`;
- a model may fall in several groups, and an attempt is held when any paused group covers its model;
- when the signal names no group, or a group the binding does not map, the pause covers the whole resource.

The pause is applied when the limit is observed, monotonically, without waiting for the attempt to terminate. The admission pass admits no attempt whose model a paused group of its quota resource covers, and after a pause expires it releases waiting attempts paced, a few per interval, so overdue waiters after a restart do not all start at once.

**Internal retries are bounded liveness.** Signals that the harness is retrying internally count as liveness, so `stall_after` does not fire while the harness visibly retries. The adapter sets the harness's own retry ceiling. The session runner stops a session that reports a rejected limit whose reset lies beyond `stall_after`: it interrupts the session, which ends through the normal collection and publication lifecycle so partial work is kept, and the attempt fails with `provider_limited`. A harness that retries silently, with no event, is indistinguishable from a stall and is classified `stalled`.

**Deadlines still apply.** Waiting for a reset counts towards the run's and the sub-graph's inherited deadlines (ADR-0022), which stay the user's wall-clock limits. Per-attempt deadlines are unaffected.

## Alternatives considered

- **Count provider limits against the node's attempts.** Rejected: a limit says nothing about the agent's work, and a multi-hour quota would always exhaust a policy sized in minutes.
- **Wait without a ceiling.** Rejected: a revoked or exhausted account would hold runs indefinitely.
- **Pause by namespace, alias and model.** Rejected: two aliases on one account share a limit, and several models can share one quota; the declared quota resource and groups follow the provider's actual limit.
- **Pause at terminal completion.** Rejected: an attempt whose publication or closure stays unresolved would leave other runs on the same quota unpaused.
- **Rely on jittered wake times alone.** Rejected: after a restart every overdue waiter would be admitted in one pass; release is paced at admission instead.
- **A fallback model in MVP.** Deferred: a silent model change alters quality and cost. If it returns, it will be explicit and recorded.
- **A provider quota pool with token-rate accounting.** Deferred with finding A12 of the architecture critique of 2026-10-16; the pause covers the retry storm without counting tokens.
- **A `wait` row as the pause.** Rejected: a pause belongs to no node instance; it is a small table of its own.

## Consequences

- One error class, one retry policy field, one table (`provider_pause`), one persisted node deadline (`provider_wait_deadline`) and one binding block (`quota`) are added.
- The adapter mapping is a hypothesis until the first slice records a trace per harness, as `docs/roadmap.md` states.
- Code-host credentials (issue #43) are a different identity and are not involved.

## Where it is applied

- [ir.md §6](../../knowledge/specs/ir.md#6-retry-policy-and-strategy-fr-63-fr-64): the error class and `runtime.provider_retry`.
- [scheduler.md §4, §6, §7](../../knowledge/specs/scheduler.md): liveness, the admission pause and retry handling.
- [protocols.md §3, §13, §14](../../knowledge/specs/protocols.md): the adapter classification, contracts and mapping rows.
- [data-model.md](../../knowledge/specs/data-model.md): `provider_pause`.
- NFR-2.6.
