# Browser runner heap tolerance and retention sensitivity

Checked 2026-10-22. Issue #179 and PR #180 own this measurement change. The owner approved the bounded method in [the implementation amendment](https://github.com/realkarych/kora/issues/179#issuecomment-6327372790). This record describes the candidate method and historical evidence; it does not claim a measured tolerance or runtime validation pass.

## Evidence and motivation

Independent reconstruction of all five archives from [run 39670462595](https://github.com/realkarych/kora/actions/runs/39670462595) verified their SHA-256 digests, all thirty complete workloads and all 1,680 raw summary fields. Two terminal Node heap deltas were positive: 22,768 and 14,880 bytes. Their cause remains unproved. All other original terminal heap deltas were nonpositive.

Absolute first-to-sixth terminal heap nevertheless increased by 695,696–720,760 bytes in every runner. The next baseline was 245,792–355,144 bytes above the preceding terminal sample, and late slopes were 67,172–81,232 bytes per full workload. Per-trial subtraction excludes those intertrial allocations. This is evidence of an incomplete measurement boundary, not attribution to a product leak. Five Node processes are the independent clusters; their thirty trials are not independent Node repetitions. Both arms built identical product assets.

The subsequent [terminal diagnostic](https://github.com/realkarych/kora/actions/runs/39675344484) remains calibration-ineligible and causally inconclusive. Its checkpoints and snapshot collection perturb the observation. Snapshots trigger garbage collection and comparable snapshots are needed to identify changes ([Chrome snapshot documentation](https://developer.chrome.com/docs/devtools/memory-problems/heap-snapshots)). Neither historical batch nor the diagnostic can fit the new method.

## Fixed protocol

`invocation-heap-v3` preserves three pairs, 2/20 cold loads, 3/30 lifecycle cycles, twenty-six phases, every original natural/pre-GC/terminal sample and every existing collection sequence. After persisting terminal evidence, completing raw reconstruction and persisting its compact summary, the asynchronous completion helper returns without carrying trial data. The caller performs exactly two collections separated by event-loop turns and records the post-finalization memory point. It does this after every trial, including the sixth, plus once before the series. Summary sidecars are reread only after the last checkpoint.

Seven exclusively written, bounded records bind protocol, sequence, arm/repetition, memory and explicit retention ownership. Parsers require closed keys and nonnegative safe-integer counters. Reads use nofollow/nonblocking descriptors, regular-file checks and a 4 KiB checkpoint bound. Missing, reordered, duplicate, malformed, oversized or substituted points fail. Raw pending/terminal/trial records remain independently reconstructible. No adaptive warm-up, additional collection until pass, byte subtraction or code-counter correction is allowed.

Let H1 through H6 be chronological post-finalization heap values. The additional gates are:

- Full upward excursion D = max(0, Hj − Hi), over all i < j.
- Late upward excursion L = max(0, Hj − Hi), over 4 ≤ i < j ≤ 6.
- Signed late slope S = median((Hj − Hi)/(j − i)), over 4 ≤ i < j ≤ 6, in bytes per full workload. Gate max(0,S), retain S. With three equally spaced points this equals (H6 − H4)/2; it is not an independent robust trend estimate.

Upward excursions ignore pure release and still expose renewed growth below an earlier high point. Original forty-eight metric families and their 336 decisions per invocation remain. Invocation gates are additional.

## Candidate arithmetic and ceilings

For five values V, define F(V) = ceil_to_4_KiB(max(0, max(V) + 3 × MAD(V))). MAD is the unscaled median absolute deviation about the median. The five fitting inputs are independent runner values:

| Budget | One value from each runner | Ceiling |
|---|---|---|
| Terminal T | Signed maximum of all six original terminal heap deltas | 60 KiB |
| Full growth | D | No separate ceiling; late gates remain mandatory |
| Late excursion | L | 60 KiB |
| Late slope | Signed S, without premature clamping | 30 KiB/workload |

Rounding occurs before ceiling checks; no clipping. The largest accepted slope grid value is 28 KiB. A raw 30 KiB estimate rounds to 32 KiB and fails. Signed inputs matter: −8, −4, 0, 4, 8 KiB produce a 20 KiB budget; premature clamping would produce 8 KiB. T applies to both arms' absolute terminal heap gates and the existing paired rule: median difference ≤ T and largest difference ≤ 2T after the existing nonnegative clamping.

Training reports await batch aggregation and cannot freeze a budget themselves. Every training runner must pass the resulting ordinary and invocation gates. Unexplained continuing growth, an exceeded ceiling or any unrelated failure rejects the candidate. The other forty-seven families retain their formulas, including terminal external/ArrayBuffer zeros and six two-byte floors. A learned increasing baseline cannot waive the late ceilings.

## Real sensitivity and recovery controls

Explicit CLI choices select real arrays filled with noninteger doubles. V8 stores pure-double elements as raw doubles ([V8 representation](https://v8.dev/blog/fast-properties), [element kinds](https://v8.dev/blog/elements-kinds)). The declared payload uses eight bytes per element, distinct from physical container overhead. Arrays remain owned across trials and both arms. No Buffer, external allocation, patched counter or payload serialization is used.

Cycle controls allocate 1, 8 or 32 KiB on each of thirty measured cycles per trial: exactly 180 arrays across the invocation, capped at 5,898,240 nominal bytes. Intertrial control allocates 240 KiB after each original terminal sample during finalization: exactly six arrays, capped at 1,474,560 bytes. Cleanup releases the owner idempotently on success, failure and cancellation.

The required 8/32 KiB and intertrial controls must fail at least one invocation gate. Only absolute/paired failures of runnerheapUsedSpan, runnerheapUsedLateSpan, runnerheapUsedMedianGrowth, runnerheapUsedPeak and runnerheapUsedReleasedGrowth may coexist as ancillary failures; they do not themselves prove detection. Every unrelated gate, evidence check and ownership/recovery check must pass. The 1 KiB outcome is reported without requiring detection. An abort, malformed data, incomplete workload or generic failure is never a successful leak control.

Once, after the intertrial control has completed and released all primary payloads, a same-process bounded fixture records B after fixed collection, allocates one 240 KiB array, holds it through six fixed-collection points C1–C6, releases it once, then records R after fixed collection. All eight observations and ownership states are persisted. Production invocation gates must pass on C1–C6. Counts are one/240 KiB at C and zero at B/R. Observed allocation and release must each be at least 240 KiB − T; final heap recovery R − B must be ≤ T, and external/ArrayBuffer recovery nonpositive. Slope units here are bytes per fixture step. It proves startup-step/plateau/recovery only, not acceptance of arbitrary late steps. No browser workload or fifth control job is added.

## Finite validation and provenance

The fixed product comparison base is `b3aa12eea8a27fc22643e88bb5a82939481f42bc`. Accepted process-only changes #187/#188 are integrated separately; authenticated checkout-parent identity is distinct from this measurement base. The checkout tree must equal the PR head tree.

One workflow attempt supplies exactly five independent Linux training jobs. Expected source identities, names and slots are declared before dispatch; server-assigned run/job/artifact IDs are bound from authenticated GitHub metadata before numerical inspection. The whole pipeline stays below ten minutes, with the other lanes, pinned actions/tools and read-only permissions preserved. Any failed or incomplete job consumes the batch.

Only if all training and independent admission checks pass is its aggregate frozen once. A separate single attempt supplies five reversed-order holdouts and four distinct controls. Every report binds the complete frozen aggregate digest. Admission reconstructs the training aggregate and every raw holdout/control summary, all 336 decisions, seven checkpoints, invocation gates, control classification and bounded recovery observations. Source changes between stages may contain only the frozen budgets, durable evidence and workflow stage; collector, evaluator, control, toolchain and product inputs remain identical. Stage substitutions, duplicate/missing jobs, unexpected attempts and changed candidates fail.

All five holdouts and all required controls must pass their declared predicates. Any miss, unexplained non-plateau, exceeded ceiling, incomplete evidence or pipeline overrun stops the procedure without retry, replacement, additional diagnosis or expanded allowance. Failed evidence never becomes training data. After acceptance, restore the single ordinary verification job and obtain final-revision CI and independent reviews.

## Limits and trust boundaries

Aggregate Node heap includes runtime and harness objects. External memory and ArrayBuffers overlap, and RSS includes additional native/code/allocator memory ([Node memory counters](https://nodejs.org/api/process.html#processmemoryusage)); these counters must not share a blanket tolerance. Exact ownership release stays exact even for a very small reproducible leak.

The 8 KiB-per-cycle target is 240 KiB per thirty-cycle trial. It is an engineering sensitivity target, not a promise to detect every smaller, intermittent, late-onset or offsetting leak. The 1 KiB probe and original signed readings remain visible. Five clean holdouts cannot certify a small false-alarm probability: under independent identical trials, their one-sided 95% upper bound is approximately 45.1%; 59 clean trials would be needed for an upper bound below 5%. That larger experiment is outside this approval ([exact binomial intervals](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm), [measurement levels](https://kar.kent.ac.uk/33611/)).

TM-12 governs verification integrity, TM-08 candidate/evidence binding, and TM-10/TM-11 bounded artifact admission. New mode/parser/control diagnostics use fixed content-free failure classifications. Payload arrays never enter artifacts. Existing ownership, environment isolation, bounded capture and cleanup obligations remain.

## Current evidence status

Planning reviews passed for runtime correctness, performance and security; verification correctness and security; and separate runtime/policy condensation. Source review, authenticated training, sensitivity/recovery validation, final-revision CI and owner line review of docs remain pending. No tolerance has been frozen.
