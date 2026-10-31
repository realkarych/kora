# Ordinary-node backoff numerical contract and design evidence

Date: 2026-10-31. Decision: package A, accepted by the project owner under [issue #568](https://github.com/realkarych/kora/issues/568) and its [direct adoption receipt](https://github.com/realkarych/kora/issues/568#issuecomment-6541035647).

## Accepted contract and publication scope

The [scheduler numerical contract](../specs/scheduler.md#ordinary-node-backoff-numerics) is the normative definition. It contains all eight common clauses and the ceiling quantizer selected from proposal SHA-256 `6df381fb5680086ed2ba8d2b2aabd04e62a727590691791fd23e94f274242d49`. The selected text has SHA-256 `1be0657ff5e5a8bfe20c61ddd4e8cce3eb59e341ba193cedbcb018f8d94fe153`. The [publication plan](https://github.com/realkarych/kora/issues/568#issuecomment-6541092977) and its [full-read disposition](https://github.com/realkarych/kora/issues/568#issuecomment-6541137897) limit this change to that contract, the [IR cross-reference](../specs/ir.md#6-retry-policy-and-strategy-fr-63-fr-64) and this record.

The existing symbolic formula did not separately define its count, the interpretation of a saved finite coefficient or fractional-microsecond rounding. These are newly accepted numerical choices, not evidence of pre-existing runtime behavior. The example intervals and coefficient in IR section 6 are not defaults.

The selected count is consumed ordinary-policy attempts including the just-failed attempt, not physical attempt numbering or an inference from partial history. Infrastructure and provider-limited attempts remain outside it. The calculator domain is `1..100`; actual eligibility requires `n < max_attempts <= 100`, so an eligible ordinary retry has exponent at most 98 while the broader calculator domain has exponent at most 99. This creates no upper bound on stored history or the general eligibility facts.

Intervals are already resolved exact nonnegative integer milliseconds, including zero and a maximum below the initial interval. An integer coefficient retains its mathematical integer value. A finite binary64 coefficient denotes the exact rational value already decoded, without reconstructing original decimal digits or rounding a floating-point power. The exact rational delay is capped before one final ceiling to arbitrary-precision integer microseconds. This is a delay result, not a timestamp or effect permission.

Nearest ties-to-even and floor were alternatives in the original proposal. The owner selected ceiling, which never shortens the exact nominal delay and adds less than one microsecond. The other quantizers are not adopted rules. The integer-millisecond maximum converts to an integer number of microseconds, so the final ceiling cannot exceed that cap.

## Exact method and bounded argument

One candidate implementation method represents `b` as exact positive integers `p/q`, begins with `min(I,M)` and performs at most `n−1` exact multiplication/comparison steps. For an unsaturated running rational `a/d`, compare `a*p` with `M*d*q`; reaching/exceeding the latter returns `M`, otherwise retain `(a*p)/(d*q)`. Reduction by the integer greatest common divisor is allowed; intermediate quantization is not. Since `b >= 1`, this recurrence is algebraically equivalent to the accepted formula's exact `min` and is monotone until saturation. No uncapped `pow` is needed.

For a stated work/space bound, let `L_M`, `L_p` and `L_q` be the bit lengths of `M`, `p` and `q`; let `k <= 99` be the step count. Without even requiring fraction reduction, the denominator has at most `1 + k*L_q` bits. Before a step an unsaturated numerator is less than `M*d`; multiplication and cap comparison therefore have bit lengths bounded by `L_M + 1 + k*L_q + L_p + L_q` plus a constant. The initial comparison additionally depends on the admitted initial interval's bit length. For integer coefficients `q=1`, saturation prevents retaining a huge uncapped power across steps. Final quantization uses one exact division/remainder operation. These are bounds as functions of existing input lengths and the existing count bound, not claims of constant CPU, fixed RAM, a universal latency guarantee or an approved numerical benchmark. No new maximum interval/coefficient or decimal-digit limit is proposed.

The recurrence needs no fraction reduction to establish those bounds. An enormous integer coefficient may still require an enormous first product; its size is included through `L_p`. Unsaturated inputs retain size-dependent exact-arithmetic costs. Avoiding uncapped powers does not establish denial-of-service resistance or the operational allocation/timing behavior of a future shared-process consumer.

## Algebraic examples

These examples are hand-derived, not executed experiments or tests. Their coefficients are exact binary fractions, isolating quantization from decimal-input interpretation.

| Resolved facts | Exact capped microseconds | Selected ceiling result |
|---|---:|---:|
| `I=1ms`, `b=1.5=3/2`, `n=5`, `M=1s` | `1000 × (3/2)^4 = 5062.5` | 5063 |
| `I=1ms`, `b=1.125=9/8`, `n=3`, `M=1s` | `1000 × (9/8)^2 = 1265.625` | 1266 |
| `I=30s`, `b=2`, `n=1`, `M=10s` | 10000000 | 10000000 |
| Zero initial interval or zero maximum | 0 | 0 |

## Independent design evidence and original inputs

The [complete independent design results](https://github.com/realkarych/kora/issues/568#issuecomment-6539765213) were returned before owner adoption on 2026-10-31. Their aggregate SHA-256 is `9762822be20581f4c9064f43e03db6f866b4faa187082ed551cbddf7d2d45b5d`. They inspected the complete proposal identified above and its decision plan, SHA-256 `f7da8592570197682ea323903a15a4c44671fb41b177824f159c3d9f82cb28ff`, against source revision `2b468c836e381e1b487effa5d455b5e74bb53d01`, tree `105838f90a0cf0ea188de84161af7ccb0878faeb`.

| Independent profile | Actual design conclusion and scope | Preserved report SHA-256 |
|---|---|---|
| Correctness | Passed, no concrete finding; count, cap equivalence, all three one-final-quantization packages, zero/cap ordering, simplicity and future regression value | `2f96aacc467607094fa6ca0b338d918acbc2d96564980aa7b7a3daf8983e51e3` |
| Security | Passed, no concrete finding; admitted numeric magnitudes, saved-value provenance, pure-intent/refusal boundaries, safe diagnostics and preserved effect/recovery authority | `ac8212a7752b4929c76b439597e65fa42dd196cb627257da8100bdc4d073ad9a` |
| Performance | Passed, no concrete finding; exact recurrence, saturation, retained/comparison temporary bounds, input-bit-length dependence and all three quantizers | `fb529ce82bfc5c236d2898f5c2e71da1c4b36eb6e528336bb56981fa783b955b` |

Each independent profile matched all ten source blobs below and inspected the applicable contracts and governing decisions. The original report identities, full coverage, restrictions and limitations remain in the linked actual results. No prior eligibility review supplied numerical-method coverage. No descendants, project/proposal code execution, tests, experiments, benchmarks or measurements were used in these design reviews. A later package choice and publication SHA do not turn them into source-implementation or runtime results.

| Pinned source at the reviewed revision | Git blob |
|---|---|
| `knowledge/specs/scheduler.md` | `2014067bf9215068f93872c50d32edd40b241f22` |
| `knowledge/specs/ir.md` | `7682b02b8a3f7ff59f6dc81057583a5c55ddf6a1` |
| `src/kora/ir/config.py` | `9c2a427d71622ce75dfe94666212f87b3df55ed8` |
| `src/kora/ir/scalars.py` | `cebef0d76328a8a119397bf5905f5074ec17c316` |
| `src/kora/db/types.py` | `068d4f8abe922d8a1b5bc90ed4a47a774e4e3e1a` |
| `src/kora/engine/retry_models.py` | `3b0efc6aa2982e1216565a3fb120b6ca40123ef5` |
| `src/kora/engine/retry.py` | `a1f25dfb4ccd19da1cfe8d2e4c2217aada27fbfe` |
| `docs/decisions/0007-attempt-model-and-retries.md` | `4a43d3c4aae44d028d12479956e28584006945b3` |
| `docs/decisions/0022-inherited-execution-deadlines.md` | `26c15539bd2fd39779e6d07a903ad5e27bcd6c5b` |
| `docs/implementation-plan.md` | `b716d339fd92fc76a72ad6248598d06073b67ad3` |

[ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md), [ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md), [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md), [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md) and [ADR-0024](../../docs/decisions/0024-provider-limits-as-non-agent-failures.md) retain their governing authority. The [first-slice refusal](../specs/first-slice.md#refusal-contract), [saved execution contract](../specs/data-model.md#111-graph-version-contract) and [implementation ownership](../../docs/implementation-plan.md) remain unchanged.

## Unapproved integration and activation prerequisites

Contract publication implements no arithmetic calculator, normalized policy producer, count derivation, clock sampling, timestamp addition, wait persistence, attempt/counter transition, replay or retry activation. An integer delay cannot establish its saved provenance or authorize an effect.

The authoritative engine/scheduler owner must separately refuse an unrepresentable timestamp sum before retry mutations, without wrap, clamp, truncation, shortening, infinity or execution-closure inference. Its node outcome, error class, event reason, persistence and public mapping remain unapproved. Authentic saved-numeric production and serialization preserving the adopted interpretation under an accepted immutable semantics binding also remain unapproved. No format, codec, version, default or external failure disposition is selected by this publication.

Actual ingress admission/decoding limits, normalization, authoritative ordinary-policy counts, atomic waits/transitions and recovery, diagnostic negative cases, supported-platform/database evidence and runtime activation require their owning implementations. Missing saved policies retain fail-closed compatibility; current defaults cannot repair history. Original failure classes, first-slice prohibition, runtime/provider separation, immutable clocks, exact closure, retained reservations and capacity admission remain binding.

Correctness/security/performance implementation results, allocations, CPU/RSS/latency, NFR-3 scheduling performance and denial-of-service resistance remain pending or unverified; none is measured passing here. A pure calculator is separate future enhancement work after contract delivery. The existing estimate of 140–220 production lines is not inspected source size or authorization. There is no combined integration estimate; revisit each unsupported part only with its concrete owner-approved contract and scope.
