# Performance review

Act as an independent reviewer of the selected performance scope. Read `AGENTS.md`, affected contracts and NFR-3 in `docs/requirements/non-functional.md`. Follow the parent skill's scope, independence, reuse and read-only rules. Native GitHub review inspects source and retained evidence without executing candidate code. Local execution requires the assignment's permission. Return concrete findings, observed results and limitations; do not expand the assignment into unrelated profiling.

## Choose the assertion

Separate three questions: whether the component releases its owned resources, whether a repeated workload shows persistent component retention, and whether its operational time/memory envelope satisfies an approved requirement. Passing one does not prove the others.

Exact resource ownership, product quotas and deterministic algorithmic/work/space limits are blocking contracts. Shared caches, allocator high-water state and runtime bookkeeping need explicit owners and finite bounds; do not treat every nonzero heap/RSS difference as a leaked resource. A known cache explanation does not excuse unexplained component growth.

A blocking numerical performance assertion states its requirement, input sizes/concurrency, environment, warm-up/cache model, observer effects, metric, minimum material regression and decision rule before execution. Use representative measured baselines or an explicit implementation/product bound. A percentage above one adverse observation is an engineering allowance, not a false-positive model or proof of leak freedom. Do not introduce such a gate merely because a counter can be collected.

Existing approved gates remain blocking until the owner explicitly approves their named replacement or removal. Preserve failures as failures. New or unsupported numerical assertions remain diagnostics with stated limits; they cannot be called passing performance guarantees. NFR-3 scheduling/scale requirements and previously approved component-specific gates are not removed by this classification.

## Resource lifetime evidence

1. Inventory acquired resources, owner, bound and actual release condition. Include relevant tasks, threads, connections, descriptors, subprocesses, temporary payloads and caches. Distinguish unresolved safety/recovery state from disposable resources; cleanup cannot destroy required evidence.
2. Select representative success, real failure, cancellation, timeout, recovery and shutdown paths from the changed lifecycle. Use real components and configured protocol fakes under repository rules. Name the question each additional workload answers; no arbitrary cycle-count quota replaces that question.
3. Wait for explicit completion with deadlines before sampling. No synchronization sleeps, production cache clearing, test-only production cleanup or repeated process restart that conceals accumulation. A test-only runtime-cache normalization experiment is diagnostic and must not replace ordinary-process memory samples.
4. Use exact ownership/count checks for resource release. For persistent growth, use attributable allocations/retaining paths and an explicit convergence or trend criterion that accounts for bounded caches and observer overhead. GC counts omit native/untracked allocations; flat RSS omits live-resource identity. Use complementary counters where the changed risk requires them.
5. Measure operational peak/settled memory, time, throughput or query work only against the assertion selected above. Record all declared samples and unavailable counters. A diagnostic outlier may justify one concrete follow-up hypothesis; it is not an automatic new merge blocker or a license to optimize dependencies until a small threshold turns green.
6. Preserve workload, environment, tested source identity, commands, failures and cleanup evidence in bounded artifacts with a concise durable method/limitation record. Reuse unchanged component measurements according to the parent skill; avoid another full campaign for a revision-only or editorial change.

Use controlled, comparable runners for numerical regression decisions; disclose shared-host load for exploratory observations. Do not discard adverse samples or rerun unchanged campaigns for a favorable result. If the method cannot separate the claimed defect from runtime/environment effects, report the assertion as unverified and propose a better method instead of silently widening its threshold.

## Component obligations

- Backend: inspect changed complexity, bounded query/stream work, concurrency and fairness. Exercise relevant SQLite/PostgreSQL behavior. NFR-3 scheduling latency and 1,000-node materialized IR obligations activate with their actual consumers; startup is reported separately.
- Browser: preserve the approved numerical exclusion and the functional/resource-ownership checks in the [component checklist](../../../../knowledge/reviews/ai-development-setup.md#browser-benchmark-exclusion). Changed browser behavior selects browser review independently.
- Diagnostics: preserve the [bounded output and failure-independence contract](../../../../knowledge/specs/observability.md#6-bounded-output-and-failure-behavior). Check content protection, bounded buffering, actual release and continuing core progress for changed sinks/collection. Do not reactivate every logging workload for an unrelated provider change.
- Local conformance tooling: preserve the owner-approved [local-tooling qualification separation](../../../../knowledge/reviews/ai-development-setup.md#local-tooling-lifetime-qualification). Ordinary lifecycle regressions, including required Linux full-disk cases, remain mandatory per PR. The instrumented 240-cycle study runs nightly/manually and qualifies releases for their exact revision; it is not a PR merge gate. This narrow exception does not move storage or other component checks to optional schedules.
- CI: retain hard job/test/cleanup deadlines and required outcomes. Record the whole-pipeline duration against the ten-minute feedback target. A target miss is maintenance evidence; it does not invalidate passing integrity checks or authorize dropping their coverage.

Missing evidence for an approved blocking requirement is unverified or blocked. Diagnostic results describe only their measured workload and carry no general latency, RSS, throughput or leak-freedom guarantee. Further investigation must answer a named unresolved acceptance question and have a stopping condition.
