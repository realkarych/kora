# Browser runner heap tolerance and retention sensitivity

Historical method: the owner-approved [removal amendment](https://github.com/realkarych/kora/issues/179#issuecomment-6328536178) withdraws this Node-controller self-memory protocol and its guarantees. The failed v3 evidence below remains rejected and causally unresolved. The later [complete benchmark removal](https://github.com/realkarych/kora/issues/179#issuecomment-6329273349) also retires v4. Current browser verification uses [console-functional-v1](console-browser-checks-2026-10-21.md#current-console-functional-v1-protocol); this record supplies no current budget or sensitivity guarantee.

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

Only if all training and independent admission checks pass is its aggregate frozen once. A separate single attempt supplies five reversed-order holdouts and four distinct controls. Every report binds the complete frozen aggregate digest. Admission reconstructs the training aggregate and every raw holdout/control summary, all 336 decisions, seven checkpoints, invocation gates, control classification and bounded recovery observations. Source changes between stages may contain only the frozen budgets, durable evidence and workflow stage; collector, evaluator, control, toolchain and product inputs remain identical. The complete workflow must match the exact four-field transformation of job name, explicit closed matrix, invocation command and artifact name; a filename allowlist alone is insufficient. Every other workflow byte, including action pins, permissions and other jobs, remains identical. Stage substitutions, duplicate/missing jobs, unexpected attempts and changed candidates fail.

All five holdouts and all required controls must pass their declared predicates. Any miss, unexplained non-plateau, exceeded ceiling, incomplete evidence or pipeline overrun stops the procedure without retry, replacement, additional diagnosis or expanded allowance. Failed evidence never becomes training data. After acceptance, restore the single ordinary verification job and obtain final-revision CI and independent reviews.

## Limits and trust boundaries

Aggregate Node heap includes runtime and harness objects. External memory and ArrayBuffers overlap, and RSS includes additional native/code/allocator memory ([Node memory counters](https://nodejs.org/api/process.html#processmemoryusage)); these counters must not share a blanket tolerance. Exact ownership release stays exact even for a very small reproducible leak.

The 8 KiB-per-cycle target is 240 KiB per thirty-cycle trial. It is an engineering sensitivity target, not a promise to detect every smaller, intermittent, late-onset or offsetting leak. The 1 KiB probe and original signed readings remain visible. Five clean holdouts cannot certify a small false-alarm probability: under independent identical trials, their one-sided 95% upper bound is approximately 45.1%; 59 clean trials would be needed for an upper bound below 5%. That larger experiment is outside this approval ([exact binomial intervals](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm), [measurement levels](https://kar.kent.ac.uk/33611/)).

TM-12 governs verification integrity, TM-08 candidate/evidence binding, and TM-10/TM-11 bounded artifact admission. New mode/parser/control diagnostics use fixed content-free failure classifications. Payload arrays never enter artifacts. Existing ownership, environment isolation, bounded capture and cleanup obligations remain.

## Current evidence status

Planning reviews passed for runtime correctness, performance and security; verification correctness and security; and separate runtime/policy condensation. Local static checks and 685 SQLite/PostgreSQL tests pass. Runtime source reviews identified distinct recovery-failure reporting and production retained-owner failure evidence as required refinements; policy review additionally requires exact workflow-content binding and exactly seven checkpoint files. Those refinements preserve the finite execution contract. The runtime environment factory no longer constructs unused resource owners merely to obtain the unchanged child allowlist.

Real runner cancellation and a parent-created report-path collision each acquire one 8 KiB payload and exercise the production outer-finally release. Both record one allocation with zero remaining arrays/bytes after cleanup; report failure cannot bypass that release. CLI and malformed/assertion-bearing manifest fixtures inspect actual process output for marker exclusion. Recovery reports distinguish invalid evidence, failed recovery, unrelated failures and required detector misses. Local smoke, including all failure/cancellation/network/artifact checks, passed in 13.945 seconds before the final evidence-admission refinement. These are functional/fixture results, not calibration.

Final source re-review passed at the measured revision below. The single authorized training attempt then failed in every runner. Sensitivity/recovery validation was not dispatched. Final readiness and owner line review of docs remain pending; no tolerance has been frozen.

## Rejected invocation-heap-v3 training attempt

The single approved training attempt is consumed. [CI run 39687134851](https://github.com/realkarych/kora/actions/runs/39687134851), attempt 1, measured head `30757cb34bbe74f0582314d75d24870161a07b2d` with checkout `d0d02ebfc512c7122d82c9516e1ceaff744f40bd`. Authenticated checkout parents are `ae70c5aceee5be65d70a0e80be9416191c8cdee8` and the measured head; checkout and head trees both equal `d4cacd371f1718fbe32f9a4d44851737ce04f4ea`. Product comparison base remains `b3aa12eea8a27fc22643e88bb5a82939481f42bc`.

[Source/slot preregistration](https://github.com/realkarych/kora/pull/180#issuecomment-6327993208) preceded the push. [Run/job binding](https://github.com/realkarych/kora/pull/180#issuecomment-6328008625) preceded numerical inspection. All five artifacts were bound and their downloaded digests verified before their respective numerical inspection. The failed-job manifest SHA-256 is `504a66d07a8426c3a9026af64dbd60adf431b1e5a45a459a49a7831fe26b84c5`.

| Slot | Job | Artifact | ZIP SHA-256 |
| --- | --- | --- | --- |
| 1 | 119523945184 | 12387386168 | `ec70725ba2d57bb80a3b0ce62254f2dfe63df95bdfd53085097571329e12c49e` |
| 2 | 119523944971 | 12387012929 | `313462fd1971cb82e08b05ab84605755bb932a6529a3fa64ded23600b8e70480` |
| 3 | 119523945025 | 12387331116 | `44f09c0e39f50565587ef977e38fe6d9433c58f955054a19b5f2ce590f86643e` |
| 4 | 119523945160 | 12387346385 | `c91097ae83b58cb5ab65e1cfb87fa650f5c93d9cb139ec192091919365697561` |
| 5 | 119523945332 | 12386637475 | `9fc75e8ad7d48d5e6820ec598ae515863209419ced9092a0a8aedafbefc62367` |

All five jobs failed after preserving their six full workloads. Independent offline reconstruction matched all thirty pending/terminal/final trials, 1,680 summary fields and thirty-five invocation checkpoints. No artifact was substituted or excluded. The report field `eligibleForCalibration: true` identifies the requested mode; failed status prevents admission.

| Slot | Maximum original terminal heap delta, B | Full excursion D, B | Late excursion L, B | Signed slope S, B/workload |
| --- | ---: | ---: | ---: | ---: |
| 1 | 914032 | 967848 | 256552 | 128276 |
| 2 | 906384 | 981536 | 300320 | 150160 |
| 3 | 980584 | 972728 | 278288 | 139144 |
| 4 | 944656 | 995064 | 304896 | 152448 |
| 5 | 923272 | 966912 | 296648 | 148324 |

Twenty-six of thirty original terminal heap deltas are positive, 806,472–980,584 bytes. Four initial trials release 6,230,112–6,269,824 bytes. Every post-finalization H1→H6 transition is positive; late transitions are 107,680–180,824 bytes. There is no observed late plateau. Every runner already exceeds the 61,440-byte terminal and late ceilings and the 30,720-byte/workload slope ceiling, without fitting any budget.

The direct runtime rejection is `requireCandidateHeap` in `protocol.ts`, called after complete summaries in `run.ts`. Its terminal heap assertion fails. The safe `invalid-evidence` error class does not mean the raw records are malformed. Terminal external deltas are −8,518…0 bytes and ArrayBuffer deltas are exactly zero, so those preceding release checks do not explain the rejection.

Resource records provide narrower positive evidence: all thirty trials retain the 2/20 load, 3/30 cycle and twenty-six-phase workloads; fifty termination proofs and eighty exact-cleanup records reconstruct without discrepancies. Owned descendants and proxy sockets reach zero. Each retained cancellation and report-collision probe acquires one 8 KiB array and records release to zero. Browser DOM/document/listener spans are zero; browser post-GC heap spans are 172–228 bytes.

Causal attribution remains unverified. Both arms have identical product assets. Browser snapshots do not provide Node retaining paths, and aggregate Node/RSS counters do not identify a particular application, harness, runtime or cache defect. No larger tolerance is justified by this result.

The registered admission command `pnpm --dir web run calibrate:browser MANIFEST OUTPUT` exits 1 at manifest admission for the actual failed job conclusions; it creates no aggregate. The 336 ordinary gate decisions per runner were not produced or accepted. No v3 budget was fitted or frozen, and no holdout, positive-control, recovery-validation, replacement or additional diagnostic invocation was dispatched. The 8 KiB/cycle detection target remains unverified.

All eight other CI jobs and Gitleaks, OSV-Scanner and CodeQL passed on the measured head. The whole pipeline ran from 22:31:41 to 22:37:49 UTC on 2026-10-21, 368 seconds. Browser functional smoke, source/static checks and independent source reviews do not override the failed numerical result. PR #180 remains a draft; integration into CEL PR #175 and final readiness remain blocked by the approved finite stop rule.
