# Architecture critique dispositions, October 16, 2026

This is the disposition and evidence index for the owner's October 16 architecture review, consolidated on October 18 under issue #95. The review compared repository contracts with vendor documentation/source; it ran no kora, engine or competitor system. Its numeric scores were design opinions, not measured readiness. [The complete original record](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/reviews/architecture-critique-2026-10-16.md) retains the hypotheses, counterexamples, proposed alternatives and source bibliography.

Accepted design, implemented behavior and executed verification are separate. Current ADRs and specifications below own implementation; this historical index does not override their later amendments. In particular, original proposals for remote compilation, trusted evaluators, provider quota pools and stable fan-out keys were not all accepted.

## Findings and current owners

### A01 Policy boundary

<a id="a01-policy-boundary"></a>

Accepted in issue #14: a permission prompt is not a universal pre-tool hook. [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md), [policy and harness conformance](../specs/protocols.md#12-policy-dd-29) own required-policy rejection, explicit coverage and separation from runtime isolation. `test_required_policy_is_enforced_or_rejected` must judge actual forbidden effects across supported modes, including promised subagent/MCP coverage; fake streams do not establish real hooks. Keep versioned operator probes separate from deterministic CI.

### A02 Compilation authority

<a id="a02-compile-isolation"></a>

Accepted in issue #15 by choosing author-side compilation: [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md) removes remote compilation from the control plane. [DSL validation](../specs/dsl.md) owns deterministic compilation and template validation; graph imports run without a sandbox or resource limit, with the author's own privileges. API validation accepts data. The proposed `test_remote_compile_isolated` applied to the rejected server-side implementation and is not an MVP acceptance requirement. A future browser compiler must reopen that trust decision; a “restricted interpreter” alone is not isolation.

### A03 Immutable approval evidence

<a id="a03-review-revision"></a>

Accepted in issue #16: [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md) invalidates moved candidates and requires recomputed evidence through producer retry; it rejects re-requesting on an old bundle and automatic VCS-driven downstream clear. [Scheduler approvals](../specs/scheduler.md#5-waits-hold-no-worker-nfr-13) own `test_push_cannot_rebind_old_evidence`, including concurrent push/approval, delayed response and replay. ADR-0021 adds current-wait authority and observation-order limits.

### A04 Measurement trust

<a id="a04-evaluator-independence"></a>

Partially accepted in issue #21: [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md) requires visible protected paths, base commit and digest-check outcome. In-repository gates measure behavior but do not attest evaluator independence; code outside protected paths can influence them. A separately hosted trusted evaluator is deferred. The proposed `test_candidate_cannot_forge_verification` describes that stronger boundary and must not be presented as an MVP guarantee. [IR output facts](../specs/ir.md#3-nodes) and console facts preserve provenance; agent narrative cannot substitute for gate results.

### A05 Deterministic selection

<a id="a05-deterministic-selection"></a>

Accepted for the canonical graph in issue #17: [ADR-0012](../../docs/decisions/0012-no-transform-node.md) uses command nodes for candidate packing and fixed-formula selection; the agent writes narrative. [IR worked example](../specs/ir.md#11-worked-examples) owns `test_candidate_selection_exact_and_reproducible`: threshold edges, ties, empty input, invalid/incomplete metrics and exact input-reference membership, with identical output digests for identical inputs. The graph repository owns those protected scripts; no general transform language was introduced.

### A06 Current-result authority

<a id="a06-result-authority"></a>

Accepted in issue #13: a lease epoch alone does not fence a cancelled attempt. [Data-model invalidation](../specs/data-model.md#6-clear-downstream-fr-68a) owns atomic current-pointer/desired-state revocation and `test_cleared_attempt_cannot_publish_current`, in both upload/clear orders on both databases. Late bounded history and cost survive. ADR-0017 now also prevents replacement admission until execution/dispatcher/publication closure; the old example's immediate replacement is not permission to bypass recovery.

### A07 Original execution domain

<a id="a07-execution-domain"></a>

Accepted in issue #13 and strengthened by [ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md). [Scheduler recovery](../specs/scheduler.md#3-submission-execution-uncertainty-and-recovery) owns `test_submit_crash_recovers_in_original_execution_domain`: recover the same live execution after lost submit confirmation, reject wrong-domain absence and retain uncertainty under unreachability. Lease expiry permits a fenced recovery claim; it does not prove absence or authorize another start. Replacement requires the later ADR's complete closure evidence.

### A08 Publication reconciliation

<a id="a08-durable-intent"></a>

Accepted in issue #21 as a small contract without a generic outbox. [VCS](../specs/protocols.md#4-vcs-fr-52) and conformance own `test_publish_and_open_change_request_are_idempotent` and `test_publish_reconciles_after_response_loss`: natural identities, response loss, concurrent recovery and no blind duplication. ADR-0016/0017 require trusted checkpoints and closed prior authority; ADR-0022 forbids fresh publication after expiry. A prepared result is not renewed permission, and arbitrary agent effects are not exactly once or rolled back.

### A09 Pinned execution history

<a id="a09-graph-version-retry"></a>

Accepted in issue #18: [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md) keeps retry in the pinned version and makes a version change a linked new run. [Scheduler retries](../specs/scheduler.md#7-retry-handling) own `test_latest_graph_creates_explicit_new_execution` for added gates, removed nodes and changed types. ADR-0018 supersedes the original suggestion that history can always be reconstructed from current inputs: attempts own saved immutable requests/contracts and explicit content availability. Retention does not promise perpetual reconstruction.

### A10 Checkpoint lifetime

<a id="a10-session-checkpoint"></a>

Accepted in issue #19: [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md) distinguishes execution handles, harness checkpoints and workspace snapshots. [Runtime](../specs/protocols.md#2-runtime-fr-81) and scheduler retries own `test_resume_after_original_environment_removal`. Reference local/Docker runtimes cannot resume after release; they visibly fall back to `fresh_with_feedback`, retaining published changes and whole-session usage without double-counting. Portable checkpoint export remains deferred; a stored handle is not retained bytes.

### A11 Aggregate budgets and deadlines

<a id="a11-aggregate-budget"></a>

Accepted in the milder form in issue #13: [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md) stops new scope admission while running attempts finish; aggregate USD remains soft. [Scheduler limits](../specs/scheduler.md#4-heartbeats-stall-detection-and-timeouts) own `test_parallel_attempts_share_run_budget`, `test_subgraph_budget_includes_previous_iterations` and `test_resumed_session_does_not_double_count_usage`, including incomplete/freshness-aware metering. `test_runtime_enforces_deadline_during_control_plane_partition` follows the inherited-clock model and explicit fault limits of ADR-0022, not the original node-only formulation.

### A12 Capacity and quota scope

<a id="a12-capacity-reservations"></a>

Occupying states and atomic reservations accepted in issue #13. [ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md) and [scheduler capacity](../specs/scheduler.md#6-concurrency-limits-fr-610) now own `test_created_attempt_reserves_capacity`, `test_capacity_released_once` and `test_last_slot_reserved_once`. The original two-scheduler test was deferred to high availability; current shared-admission contention requirements still apply. Provider quota pools and the proposed `test_shared_provider_pool_limits_multiple_runtimes` / `test_quota_reset_defers_admission` were deferred, not implemented or silently adopted.

### A13 Runtime/VCS composition

<a id="a13-remote-composition"></a>

Accepted in issue #20: [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md) replaces opaque matching workspace labels with typed plans and shared executors, preserving `worker_host` for FR-8.8. [VCS/conformance](../specs/protocols.md#4-vcs-fr-52) own `test_vcs_runtime_cross_product`: independent VCS/runtime implementations, no runtime-name branching, transfer and recovery where no filesystem is shared. ADR-0016 supersedes candidate-owned plan execution and publication with trusted lifecycle authority.

### A14 Target identity and operator experience

<a id="a14-target-identity"></a>

Decided in issue #23 and delivered as a design through #38: [ADR-0014](../../docs/decisions/0014-target-identity.md), amended by ADR-0019/0020, owns optional namespace-scoped immutable target identity and `allow`/`defer` admission. [Scheduler capacity](../specs/scheduler.md#6-concurrency-limits-fr-610) owns `test_same_target_triggers_follow_declared_policy`: Q follows declared policy while independent R progresses. `supersede`, `join`, caller-supplied keys and stable fan-out mapping keys were deferred. The proposed reordered-list clause was explicitly removed; fan-out identity remains positional.

## Corrections to the competitor analysis

The review withdrew the blanket claim that nobody orchestrates full coding-agent sessions. Archon is a direct comparator; GitHub Agentic Workflows, Sourcegraph Agentic Batch Changes and Open SWE/LangGraph cover adjacent portions. These are observations from the October 16 source review, not current market claims or benchmark results. [The research evidence index](../research/agentic-pipeline-orchestrators.md) owns dated sources and limitations; [the owner-reviewed Archon comparison](../../docs/archon-comparison.md) owns product comparison conclusions.

## How mature orchestrators are built, and the applicable lessons

The historical source review highlighted explicit transition ownership, durable waits, idempotent effects, isolated author code and recovery-aware compatibility. It did not establish runtime performance or justify copying an entire orchestration platform. [The engine verdict](../analysis/own-scheduler-vs-existing-engine/verdict.md) preserves the dated comparative evidence; current requirements and ADRs determine kora's obligations.

## How to decide about the own scheduler

[ADR-0001](../../docs/decisions/0001-own-control-plane.md) keeps the SQL control plane. Its **ten accepted first-slice fault checks now live in [scheduler section 18](../specs/scheduler.md#18-first-vertical-slice-acceptance)**, including the shared scenario, both-database requirement, contention, upgrade/recovery and operational-cost evidence. They remain in the checkout independently of this historical record.

Do not start a comparative engine prototype before that slice. Reconsider DBOS with its measured data; Temporal remains a deferred alternative with an explicit transition/projection ownership problem. Engine activity retries do not remove runtime submission or publication idempotency responsibilities.

## Priorities for the user-experience design

The two accepted pilots now live in [console pilot scenarios](../design/console/pilot-scenarios.md): an engineer explains approval evidence, staleness, progress and waits without database access; an operator resolves lost execution and ambiguous publication through public surfaces. [Pages](../design/console/pages.md), [states](../design/console/states.md) and [ADR-0015](../../docs/decisions/0015-web-console-design.md) own the design. Those scenarios remain acceptance criteria, not evidence that the console has been implemented.

## Evidence limits and retained decisions

No old numeric score, proposed test name, source reading or compressed disposition proves a protection or benchmark. Real core integration runs on PostgreSQL and SQLite with configured protocol fakes; critical process boundaries need real local/Docker tests. Model-provider probes stay separate and dated. Preserve the API-first control plane, deterministic outer graph, explicit attempts/lineage, outbound workers and one approval per immutable evidence set; later ADRs own their detailed limits.
