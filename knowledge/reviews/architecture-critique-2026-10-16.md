# Architecture critique record, 2026-10-16

An independent critique of the design was commissioned by the project owner before implementation starts and run against commit `d049b2b4948ff58c4651db009ab17cb02be7893d`. It covers the architecture overview, the requirements, ADR-0001 to ADR-0013, the specifications of the graph document, the DSL, the data model, the scheduler, the protocols and the API, the canonical SQL example and the research behind the positioning. Competitor documentation and selected open-source files were read; no competitor system was run. The original is in Russian; this file is its English record, condensed where the original restated the specifications, with every finding, its basis in the repository, the proposed change, the proposed acceptance test and the owner's disposition. Dispositions were decided by the project owner on 2026-10-16; each names the issue that applies it.

The critique's overall verdict: keep the construction (control plane, deterministic outer graph, full agent sessions, outbound workers, versioned artifacts, one API); the serious defects sit in a few contracts on which the reliability and security promises rest. It scored the architectural direction 7 of 10 and the readiness of the specifications for the whole MVP about 5.5 of 10, with 9 to 9.5 reachable for the architecture within the MVP boundary once the contracts below are settled, the normative documents corrected and the listed fault scenarios proven. A 10 would also require operation in production, known scaling limits and a second adopter's infrastructure integrated, which cannot be granted in advance.

## How to read the findings

Three statuses:

- **Confirmed contradiction**: two explicit norms are incompatible, or the named external mechanism does not give the promised guarantee.
- **Missing contract**: the states, conditions or authorities needed are not defined; the described scenario shows admissible undesired behaviour without proving that a future implementation would behave that way.
- **Hypothesis**: a product or engineering risk to confirm or refute by experiment.

Priorities: **P0** close before implementing the corresponding core; **P1** close before fixing the API, the protocols and the structure of the user experience; **P2** check in the first vertical slice. These are design priorities, not severities of an existing vulnerability. Test names `test_*` are proposed acceptance criteria, not existing tests. External pages without a version describe the state checked on 2026-10-16 and are not a permanent contract.

## What the critique keeps

A useful working statement of kora's purpose: **manage the engineering changes that agent sessions produce, binding every change to its attempts, cost, checks and the human decision.** The graph of sessions enables that process but does not by itself explain why an organisation should adopt another control plane. This is a proposal for sharper positioning, not a replacement of the decided scope.

Decisions the critique keeps as they are:

1. A full agent session inside a deterministic process: the model chooses how to solve the task, the platform defines the admissible transitions, checks, retries and approvals ([positioning](../../docs/positioning.md)).
2. One HTTP API with thin clients ([ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md)).
3. Workers without database access, connecting outbound over HTTPS ([ADR-0003](../../docs/decisions/0003-scheduler-and-workers-over-http.md)).
4. Python compiled to a data representation that the scheduler interprets, provided the compiler itself is isolated ([ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md)).
5. Explicit attempts, actual artifact versions and causal links between them ([ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md), [data-model.md](../specs/data-model.md)).
6. Approvals bound to content, narrative apart from facts ([ADR-0008](../../docs/decisions/0008-content-bound-approvals.md)), once the consistency gaps below are closed.
7. Honest cost incompleteness and no promise to roll back arbitrary external actions ([ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md), clear-downstream in [data-model.md](../specs/data-model.md)); these must not be replaced by an exactly-once promise for the whole process.
8. Conformance suites for the protocols ([ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md)), provided it is first shown that the promised properties are achievable and that independently written adapters are compatible.

## Area scores and what raises them

Expert scores, not an average; they describe the design as written. A weak security contract is not compensated by a good frontend library.

| area | now | reasonable target | what has to change |
|---|---:|---:|---|
| Deterministic process around sessions | 9 | 9.5 | formulas and mechanical copying of facts out of the model; successful execution separated from a usable result |
| API, scheduler, outbound workers, thin clients | 8.5 | 9.5 | recovery contradictions removed; one owner per transition |
| Python DSL and versioned graph document | 8 | 9 | compilation isolated; compatibility and support period of the document format defined |
| Protocols and extensibility | 8 | 9 | composition of two runtimes and two VCS proven; capabilities formalised |
| Attempts, lineage and retries | 7 | 9.5 | authority to publish a current output revoked atomically; exact inputs and configuration kept |
| Crash recovery and execution ownership | 5 | 9 | execution domain, authoritative lookup, fenced adoption, tests at the crash points |
| Content-bound approvals | 6 | 9.5 | immutable review revision; evidence recomputed for new code |
| Trust boundaries and tool policy | 4 | 9 | a real matrix of harness and runtime guarantees; independent verification; rejection of an unenforceable policy |
| Graph versions, sessions and upgrades | 4 | 9 | a new version as a new linked run or a full migration contract; checkpoint apart from workspace |
| Cost and admission | 6 | 9 | aggregate budget, atomic reservations, shared provider quotas |
| Justification of an own scheduler | 6 | 9 | comparison corrected; the own engine checked against one fitting alternative on the same faults |
| Model used for packing and formula selection | 4 | 9.5 | deterministic code and references to source artifacts; the model only for judgement and prose |
| Base stack: Python, PostgreSQL, React | 8 | 9 | real integration checked; no large technology change needed now |
| User experience around targets and results | 6 | 9 | a stable target of work, its history, an attention queue and evidence next to the decision |

## Findings to settle before the core is implemented

### A01 A real security boundary instead of a universal permission hook

<a id="a01-policy-boundary"></a>

**Priority P0. Status: confirmed mismatch between the chosen mechanism and the promised guarantee.**

**Basis.** [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md) and the policy protocol in [protocols.md](../specs/protocols.md) section 12 require the policy to run before every tool call. For Codex the mechanism is `item/permissions/requestApproval` under `approval_policy = on-request`; for the future ACP adapter `session/request_permission`. The policy conformance suite requires that a deny reaches every call under every permission mode. The harness mapping table (section 14) at the same time maps `permission_mode: dont_ask` and `accept_edits` to `approval_policy = never`, under which no approval request is raised; the canonical example uses `dont_ask` on every node.

The Codex app-server documentation defines `item/permissions/requestApproval` as a request for additional filesystem or network permissions from the built-in `request_permissions` tool, not a universal notification before any tool. Approval requests for command execution and file changes have their own semantics and depend on configuration. The Agent Client Protocol says an agent *may* ask for permission through `session/request_permission`; it establishes no mandatory interception point for every call. The Claude Agent SDK does have `PreToolUse`, but one adapter's hook does not prove that all adapters are equivalent.

**Risk.** If the API accepts a universal policy and reports it applied, some actions the harness allows on its own pass without kora's check. For the platform team that is a false guarantee, even where the sandbox still restricts some actions.

**Proposed change.** Separate three independent mechanisms: (1) process and environment limits the runtime enforces: reachable paths, network destinations, credentials, process rights, sockets and metadata endpoints; (2) limits of external services: token rights, MCP server capabilities, publication through a trusted component, a proxy where needed; (3) harness hooks as an additional control point, with the call kinds and modes they actually cover listed. Every required policy needs a verifiable means of enforcement. An incompatible combination of harness, version, runtime and permission mode is rejected before the run; a warning is acceptable for an optional convenience, a required namespace policy needs a refusal. This does not require a universal policy engine or replacing the adapters. Checking a tool name or argv does not prove the absence of a forbidden effect inside an allowed program; the shell-chaining ban is useful but does not replace process isolation and credential limits. A policy is a set of rights that are really enforced, not a promise to recognise any dangerous command.

**Acceptance.** `test_required_policy_is_enforced_or_rejected`: for every supported mode, an allowed and a denied file write, shell execution and external tool call; judge by whether the side effect happened, not by a deny event; for an uncovered case, compilation or admission returns a structured error; the matrix includes subagents and explicitly allowed MCP tools when the adapter promises them. Protocol tests run without a model through the harness's real interface where it allows a deterministic driver; a live smoke test is a manual experiment outside CI, recorded with the harness version. The fake harness does not prove a real hook.

**Files.** ADR-0009, protocols.md sections 3, 12 to 14, the graph validation codes, `HarnessCapabilities`, the requirements. Target for the area: 4 to 9, only after the real mechanisms are confirmed.

**Disposition.** Accepted. The contradiction between the `tool_policy` and `permission_mode` rows of the Codex mapping is confirmed in the repository; the Codex app-server semantics are verified in the issue. Applied by [#14](https://github.com/realkarych/kora/issues/14).

### A02 Isolate the compilation of user Python

<a id="a02-compile-isolation"></a>

**Priority P0. Status: missing trust-boundary contract.**

**Basis.** The API in [api-and-agent-surface.md](../specs/api-and-agent-surface.md) has `POST /compile` with Python sources. [dsl.md](../specs/dsl.md) section 2 imports the module in a restricted interpreter without network and runtime imports. The architecture forbids the scheduler to import user Python. Where and with whose rights the import through the HTTP API runs, and how filesystem, subprocesses, memory, CPU and credentials are limited, is not defined.

**Risk.** Arbitrary code at import time runs with the API's authority or reaches data of other namespaces; a well-meant graph can hang at import. "Restricted interpreter" names no operational isolation boundary.

**Proposed change.** For server-side compilation, a separate job in an isolated environment: minimal rights, no API or database credentials, a read-only input bundle, a separate output directory, resource and time limits, a bounded result; namespace policy and presets passed as immutable data; errors and results through a narrow typed interface. Alternative for the first slice: accept only a pre-compiled document and compile locally in the client environment; this changes the compile route and needs an explicit decision. Compilation must not be moved into a privileged process silently to keep the endpoint.

**Acceptance.** `test_remote_compile_isolated`: the uploaded module tries to read a file outside the bundle, open the network, loop forever and produce an oversized output; it reaches no platform data, affects no concurrent compile and ends in a structured error. Compiling one bundle twice with the same dependency versions and presets yields the same digest.

**Files.** dsl.md section 2, the compile route, the deployment diagram, the execution model of the compiler. This is the separation Airflow and Dagster make with dedicated code loading, not a requirement to use either.

**Disposition.** Accepted. Applied by [#15](https://github.com/realkarych/kora/issues/15) with the first option: the compile route is removed, compilation is a client-side function of the CLI and the stdio MCP authoring server in the author's own process, `POST /validate` is kept, and ADR-0002 records that no control-plane process imports graph authors' Python. The isolated compile job stays recorded there as the alternative for the day compilation has to run on the platform's side, for example behind a browser editor.

### A03 An approval covers one immutable revision of code and evidence

<a id="a03-review-revision"></a>

**Priority P0. Status: confirmed lifecycle inconsistency.**

**Basis.** [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md) and the `approval_request` state machine in [data-model.md](../specs/data-model.md) invalidate the request on a new push and re-request with the new SHA. The request references a specific version of the context artifact; the facts include the VCS commit and the gate results. Clearing evidence is defined for a manual retry and a new sub-graph iteration; no operation re-collects evidence for the new SHA after a push. The [design review of 2026-10-15](review-2026-10-15.md#graph-document-redundancy) removed `rerun_gates` in favour of re-requesting.

**Counterexample.** Commit A passed the tests and the bundle D(A) was built. While the reviewer reads, commit B lands on the branch. If the new request carries `(B, D(A))`, a person approves new code on old facts. If the request keeps following the bundle's reference to A, it cannot correctly approve B. Both outcomes need a rule that does not exist.

**Proposed change.** An immutable review revision defined at least by `candidate revision + input artifact versions + evaluator version + check results + policy version`. The approval references that revision whole; a new push does not change its content. One lifecycle, chosen deliberately: (1) a push invalidates the revision and recomputes the closure of nodes that produce the affected evidence; the new request is created after the recomputation; (2) the run keeps representing exactly A; checking B needs a new linked revision or run; approving A never authorises publishing B. Option 2 may be simpler for MVP; option 1 is more convenient for active work on one branch. Not every node has to be recomputed: evidence may be reused only where its independence from the changed input is proven.

**Acceptance.** `test_push_cannot_rebind_old_evidence`: A passes, B breaks the check, an authorised reviewer confirms the request for B; the old evidence does not open the gate. Also: concurrent push and approval, webhook replay, delayed response. At the moment the action is finally authorised, the current revision is compared with the approved one again.

**Files.** ADR-0008, the approval data model, scheduler waits and invalidations, the VCS approval channel, the review-revision view. Target: 6 to 9.5. This is the product's central guarantee.

**Disposition.** Accepted. Applied by [#16](https://github.com/realkarych/kora/issues/16) with the `candidate_moved` lifecycle: the data already on the request row and the bundle version make the revision immutable, and a push to the candidate branch during review moves the request to `invalidated` and fails the approval node with `candidate_moved`; nothing re-requests on the old bundle, the author retries the producing node, and the gates, the bundle and a new request are produced for the new commit through the normal downstream path. The scheduler observes the branch head through the VCS protocol for every channel, re-checks it before the approved node's downstream starts, and resolves a late response, a racing push and response, and a replayed webhook against the request's recorded SHA and bundle version. Clearing downstream from the gate nodes was rejected because a push is an external event that no node produced ([ADR-0008](../../docs/decisions/0008-content-bound-approvals.md) as amended, [scheduler.md](../specs/scheduler.md) section 5, `test_push_cannot_rebind_old_evidence`).

### A04 The verifying component is independent of the verified candidate

<a id="a04-evaluator-independence"></a>

**Priority P0. Status: risk hypothesis on an incomplete trust contract.**

**Basis.** [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md) keeps the checks in the candidate checkout and treats the protected-path check as sufficient. [ADR-0012](../../docs/decisions/0012-no-transform-node.md) has the good property that the agent writes the narrative, the platform fills the facts, and provenance distinguishes command-measured from agent-reported numbers.

**What is not proven.** That selected files are unchanged does not make the whole evaluator independent. A changed module imported by the test harness, a loader configuration, a dependency, a wrapper or a generated report can influence the run. In a benchmark the program under test is untrusted by definition; running it through a `command` node does not turn its stdout into an independent measurement.

**Proposed change.** Define the boundary of a trusted evaluator: the platform fixes the evaluator version, the measurement inputs, how the candidate is started and the channel through which the result returns. The candidate runs with the rights it needs inside that boundary but does not choose the rules and cannot rewrite the authoritative verdict. Evidence stores the source revision, the evaluator version, the input data versions, the environment and the result. A separate clean checkout is one way to protect evaluator code, not a universal solution: running the candidate with the same rights can still break trust. For ordinary trusted project tests a weaker mode may stay, with honest provenance and without presenting it as platform attestation. Hypothesis to test: "protecting a limited set of paths is sufficient for all gates of the canonical SQL scenario", checked on the real evaluator's dependencies, not on directory names.

**Acceptance.** `test_candidate_cannot_forge_verification`: the candidate keeps the protected files but changes code that could influence the test report; the trusted evaluator does not accept the forged verdict; the console shows the difference between an agent-reported observation, a project test and a trusted measurement; the result is bound to exact data and revision.

**Files.** ADR-0009 and ADR-0008, the evidence schema, the canonical graph, the runtime and evaluator boundary. Target together with A01 to A03: 9 to 9.5 for trusted results, without promising full verification of arbitrary code.

**Disposition.** Partially accepted. Applied by [#21](https://github.com/realkarych/kora/issues/21) as visibility: the protected paths in force, the base commit and the result of the digest check enter the `CommandResult` facts, the console and the change-request comment label each gate as verified against its protected paths or unprotected, and [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md) states that in-repository tests are evaluated by code the candidate can influence outside the protected paths, so a gate result is a measurement, not an attestation. In the canonical scenario the measurement comes from the warehouse through an in-house command, not from the candidate. A separately hosted trusted evaluator is deferred after MVP and recorded there as the alternative.

### A05 Remove the model from copying facts and selecting by a fixed formula

<a id="a05-deterministic-selection"></a>

**Priority P0 for the canonical scenario. Status: confirmed design decision with unnecessary non-determinism.**

**Basis.** [ADR-0012](../../docs/decisions/0012-no-transform-node.md) assigns packing, selection and bundling to ordinary agent nodes on a cheap model, with `command` as the alternative. In the canonical example the `accept` node copies commit, branch, task id and `cpu_seconds`; `select_best` picks the largest improvement of at least 15 % and returns `gain_pct`. The JSON Schema checks the shape; it does not check that the chosen commit belongs to the input set, the arithmetic, or the exactness of the copy.

**Risk.** A syntactically valid output can carry the wrong reference, a rounding, the wrong metric or a candidate below the threshold. Packing such an output into facts later does not remove the error at its source.

**Proposed change.** Keep the decision not to add a universal transform language. In the canonical graph use the existing `command` node for packing references, filtering, grouping, comparing, computing the gain and selecting. Pass the artifact reference or version id of the source candidate instead of rewriting factual fields through the model. The model stays appropriate for generating hypotheses, substantive assessment on ambiguous criteria and narrative; where a model recommendation is wanted, it returns a reference and an explanation, and a deterministic step verifies admissibility and every formal threshold.

**Acceptance.** `test_candidate_selection_exact_and_reproducible`: threshold edge values, ties with a defined tie-break, empty set, invalid baseline, incompatible units, repeated target, unknown reference, incomplete metric; identical inputs give identical results and digests. The arithmetic is a justified unit test; an integration test proves correct use of real artifact versions.

**Files.** ADR-0012, the canonical IR and DSL example, the `Candidate` and `ReviewBundle` types. Target: 4 to 9.5, a small change with a large effect on trustworthiness.

**Disposition.** Accepted for the canonical example. Applied by [#17](https://github.com/realkarych/kora/issues/17): `accept` and `select_best` are command nodes that run `tools/gates/pack_candidate.py` and `tools/gates/select_best.py`, scripts of the graph repository under its protected paths, on a checkout of `main`, over the inputs the worker materialises as `.kora/inputs/<name>.json` ([ir.md](../specs/ir.md) section 4); `accept` copies the `VcsRef`, the metrics and the hypothesis into the `Candidate`, `select_best` computes the gain over the baseline, applies the 15 % threshold, breaks ties by the lowest commit and fails when no candidate qualifies; `bundle` stays the agent node that writes the narrative; [ADR-0012](../../docs/decisions/0012-no-transform-node.md) keeps its decision against a transform node kind and states that deterministic steps are command nodes; [ir.md](../specs/ir.md) section 11 names `test_candidate_selection_exact_and_reproducible`. An agent recommendation verified by a command node is the pattern the record states for selections that need judgement; the canonical example does not use it.

### A06 Revoke atomically the old attempt's right to publish the current result

<a id="a06-result-authority"></a>

**Priority P0. Status: the described algorithm is insufficient for the stated guarantee.**

**Basis.** Lease fencing in [data-model.md](../specs/data-model.md) section 3 increments the epoch on claim and reclaim. Clear-downstream (section 6) resets node instances, marks artifacts superseded and sets `desired_state = cancel` on running attempts, then states that the lease epoch protects against late writes. Cancellation does not change the old attempt's authority: the new attempt has its own row and its own epoch. The completion pass in [scheduler.md](../specs/scheduler.md) completes a `finishing` attempt when current outputs exist.

**Scenario.** Attempt A runs with epoch 4. A manual retry upstream clears downstream and creates B. A has not yet received the cancellation through its heartbeat and uploads an output with the still-valid epoch 4. Nothing in the described conditions forbids making that output current or completing the reset node instance.

**Proposed change.** In the clear-downstream transaction, revoke the affected attempts' right to publish an authoritative result. Publishing a current artifact and completing check the instance's owner, the cancellation, the currency of the input binding and the lease. Column names do not matter; the invariant must hold on every write path. Late usage, diagnostic events and historical partial outputs stay accepted: losing the right to complete must not destroy the truthful record of money already spent. Lease ownership, result authority and historical observation are different rights.

**Acceptance.** `test_cleared_attempt_cannot_publish_current`: stop A right before upload; clear upstream and start B; let A upload and finish before its next heartbeat. A produces no current output, does not complete B's instance and unblocks nothing downstream, but its cost is kept. PostgreSQL and SQLite. Repeat for competing upload and clear in both transaction orders.

**Files.** Data model lease and clear sections, upload and finalise routes, scheduler completion. No generation counters on every table; the minimal provable mechanism is enough. Target for attempts and lineage: 7 to 9.5.

**Disposition.** Accepted. The sentence "the lease epoch fences late writes from cancelled attempts" is false in the repository because the epoch changes only on claim or reclaim. Applied by [#13](https://github.com/realkarych/kora/issues/13): clear-downstream clears `current_attempt_id`, and a `current` upload or a `finishing` transition requires `desired_state = run` and ownership of the instance.

### A07 Define the execution domain and the recovery of a live session

<a id="a07-execution-domain"></a>

**Priority P0. Status: literal contradiction plus missing contract.**

**Basis.** [ADR-0003](../../docs/decisions/0003-scheduler-and-workers-over-http.md) and [scheduler.md](../specs/scheduler.md) section 3 allow a reclaim after worker and runtime are both silent and, in the next paragraph, require adoption of a live runtime session after the worker died. The scheduler is to find out the state of runtime and worker, although [architecture.md at the reviewed commit](https://github.com/realkarych/kora/blob/d049b2b4948ff58c4651db009ab17cb02be7893d/docs/architecture.md) forbids it runtime calls and workers are outbound-only. Any worker of the runtime name may pick up the work, but a `host_path` workspace is on one machine.

**Scenarios.** The worker died and the container lives: the condition "both silent" blocks the ownership transfer. Two machines use the runtime name `docker` with different daemons: the new worker looks up the attempt in its own daemon and gets nothing, although the old session still runs on the other machine. Idempotency by attempt id holds only in the domain where the runtime actually stores and checks that key.

**Proposed change.** Define a stable execution domain: the scope in which an authoritative `lookup(attempt_id)` and deduplication refer to the same execution. It may be a runtime instance, a cluster, a daemon or a submission registry; the provider type or name need not coincide with that identity. Recovery goes through the outbound channel: the control plane hands the task to an authorised worker, the worker checks the right domain and reports its observation. Expiry of the worker's lease permits a fenced adoption of the live session; it does not require first declaring the runtime dead. Lookup semantics distinguish a confirmed absence from an unknown state due to unavailability; `KoraProtocolError` already exists, so no new DTO is needed, only a clear classification and the rule "unavailable does not mean absent". For the local runtime a weaker guarantee after the host is lost is acceptable, but it must be named and not disguised as session portability.

**Acceptance.** `test_submit_crash_recovers_in_original_execution_domain`: two hosts of one provider; crash after submit before the handle is saved; the session keeps living; inbound from the control plane blocked. With the authoritative runtime reachable and the session alive, the system adopts the same session without a second start. With the runtime unavailable or ambiguous, it keeps the uncertainty and does not resubmit. With a reliably confirmed absence, the infrastructure retry applies by policy. Absence in the wrong domain does not permit a new start.

**Files.** ADR-0003, worker registration, the runtime contract, claim routing, attempt identity, the recovery state machine. Target: 5 to 9 after tests, not after adding the word idempotent.

**Disposition.** Accepted; both contradictions are confirmed in the repository. Applied by [#13](https://github.com/realkarych/kora/issues/13): lease expiry alone authorises reclaim, the claiming worker performs lookup and adoption, adoption by another worker only for portable handles, stable worker ids, and `lookup` that raises on an unreachable runtime instead of returning `None`.

### A08 Durable intent and reconciliation for the platform's external actions

<a id="a08-durable-intent"></a>

**Priority P0 for publishing the result. Status: missing contract.**

**Basis.** The VCS protocol in [protocols.md](../specs/protocols.md) section 4 has `publish`, `open_change_request` and `post_comment`. For approval comments the scheme exists: the request row is created before the call, `approval_request.id` is the key, and on a retry the channel searches for a comment carrying that marker ([data-model.md](../specs/data-model.md) section 3). This is kept. For `publish` and `open_change_request` no comparable contract of durable identity and result lookup after an unknown outcome is defined; an idempotent runtime submit does not extend to them.

**Scenario.** The code host created the change request and the process died before the URL was written to the metadata database. On retry the platform does not know whether to create a new request or adopt the existing one. Writing the event only after the external effect does not close the window. For the approval-comment scheme, concurrent reconciliation must be checked separately: two executors must not both decide that the marker is absent and both create a comment.

**Proposed change.** Extend durable intent and reconciliation to publications kora controls: a durable operation id before the call, an unknown-outcome state, lookup of an already performed effect, retry rules. Where the external API supports an idempotency key, use it; where it does not, a verifiable correlation is needed. If the outcome cannot be established, stop at a visible unresolved effect instead of guessing. The approval-comment mechanism stays; its correctness under concurrency is proven. The implementation may use an operation log, an outbox or the existing entities; the observable properties matter, not the table name. Arbitrary agent actions inside a session remain a separate class with an honestly limited guarantee; no automatic rollback.

**Acceptance.** `test_publish_reconciles_after_response_loss`: a real local test service accepts the action, then the connection drops before confirmation. With a reliable lookup, after recovery the same single change request is bound to the result; without it the system stays explicitly unresolved and does not create a second request blindly. A repeated approval comment creates no duplicate; the race of two recovery workers is checked. A test double must be a full injected protocol implementation under conformance, never a patched HTTP transport.

**Files.** The VCS and approval-channel contracts, the durable effect state, scheduler recovery, API and console diagnostics. Temporal does not remove this task: activities may repeat.

**Disposition.** Accepted as a small contract, without an outbox. Applied by [#21](https://github.com/realkarych/kora/issues/21): the git implementation has natural keys (deterministic branch name, one change request per head and base), so `publish` is idempotent on `(repository, branch)` and `open_change_request` on `(head, base)` with a search before the creation, a retryable error after either call is an unknown outcome that repeats the idempotent call, a worker adopting an attempt between the push and the `VcsRef` upload finalises again to the same `VcsRef`, and the VCS conformance suite runs both calls twice and under a concurrent second worker (`test_publish_and_open_change_request_are_idempotent`, `test_publish_reconciles_after_response_loss`; [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md) as amended, [protocols.md](../specs/protocols.md) sections 4 and 13).

### A09 Do not change the definition of an existing run without a migration model

<a id="a09-graph-version-retry"></a>

**Priority P0. Status: missing versioning contract.**

**Basis.** A manual retry with the flag for the latest graph version in [scheduler.md](../specs/scheduler.md) section 7 re-pins the same run to a new graph version. In the data model the graph version belongs to the run; node instances and attempts carry no sufficient immutable version binding. Materialisation is described for a pending run. The API promises to compare the configurations of historical attempts across versions.

**Scenario.** V1 is `build → approve`; V2 is `build → security_gate → approve`, and at the same time an output schema or a node name changes. Old instances and artifacts keep existing while the run already references V2. Who materialises the new gate, which earlier successes stay valid and under which schema an old artifact is read is undefined.

**Proposed change for MVP.** A retry stays inside the original graph version. "Retry with the latest version" creates a new linked run. Carrying results over is possible only through explicit inputs with compatibility checks and provenance; importing old artifacts is not called a new execution of the gates. If in-place migration is ever needed, the admissible topology changes, remapping rules, the immutable execution definition of each attempt, schema compatibility and atomic invalidation must be defined first; it must not appear as a retry flag without a full model. Store a recoverable resolved configuration or a reference to an immutable object, not only a digest; define separately the semantic version of the document interpreter; additive database migrations do not prove compatible scheduler behaviour.

**Acceptance.** `test_latest_graph_creates_explicit_new_execution`: the new version adds a mandatory gate, removes a node and changes a type; the gate cannot be bypassed and an old artifact cannot be read as the new type; a historical attempt is recoverable to its exact graph, configuration and input versions. A separate test upgrades the API and scheduler during active approvals and sessions.

**Files.** ADR-0007, the graph, run and attempt bindings, the retry API, the scheduler, the version-comparison views. Target: 4 to 9.

**Disposition.** Accepted. Applied by [#18](https://github.com/realkarych/kora/issues/18): a run's graph version never changes, the retry route lost its flag for the latest version and stays within the pinned version, and `POST …/runs/{run}/rerun` creates a new run linked through `run.rerun_of_run_id` on the pinned, the latest or a named version, with the original's parameters unless overridden and consuming none of its artifacts. The resolved configuration of any attempt is reconstructible from the pinned document with its `meta.presets` snapshot, the run parameters and the consumed artifact versions, with `resolved_config_digest` as the check; `test_latest_graph_creates_explicit_new_execution` is specified in scheduler.md section 7. In-place migration stays the rejected alternative of ADR-0007.

## Contracts to settle before the protocols and the user experience are fixed

### A10 Separate the session checkpoint from the lifetime of the working environment

<a id="a10-session-checkpoint"></a>

**Priority P1. Status: missing contract; the fallback already exists.**

**Basis.** The harness defaults in [protocols.md](../specs/protocols.md) section 3 place `CLAUDE_CONFIG_DIR` inside the workspace; the composition and location of all state needed to resume are not defined. `Runtime.release` frees the workspace and container after the results are collected; cancellation also ends in release. The `resume` retry continues from the stored session handle with an explicit fallback when the handle is lost or the harness version changed.

**Risk.** The handle is stored, but the bytes needed to resume are destroyed or unreachable from the new worker. The normalised transcript is not necessarily the SDK checkpoint. The existence of the fallback does not show that regular recovery works at all after the old environment is removed.

**Proposed change.** The runtime handle and the harness session handle are already distinct. Complete the preservation contract with three lifecycles: the execution handle (a still-existing runtime job), the harness checkpoint (what a given harness version needs to continue the conversation) and the workspace snapshot (revision, uncommitted diff, needed files). Define what is exported before release, who may read it, what is portable between workers, which runtime and harness versions are compatible and when the data is deleted. Storing a digest or image version answers "what ran"; it does not guarantee that the environment can be started again. Recommended minimum: support resume only for explicitly verified pairs and states; otherwise give a reproducible `fresh_with_feedback` with the preserved changes and a visible `strategy_fallback`, without a false claim of continuing the earlier session.

**Acceptance.** `test_resume_after_original_environment_removal`: complete or interrupt an attempt, collect the state, remove the original worker or container, continue on an allowed new worker; prove either a real resume or the documented fallback with the same preserved changes; check an incompatible harness version and a missing part of the checkpoint; cost is not doubled in the session totals.

**Files.** Runtime and harness capabilities, a checkpoint artifact, retention, release semantics, the retry view. Target for versions and sessions: 4 to 9.

**Disposition.** Accepted in the honest form. Applied by [#19](https://github.com/realkarych/kora/issues/19): the `local` and `docker` runtimes declare `supports_resume = False` in MVP, because `release` frees the workspace that holds the harness checkpoint and their handles are bound to one host; protocols.md section 2 names the execution handle, the harness checkpoint and the workspace snapshot as three lifecycles and defines `supports_resume` as a checkpoint that outlives `release` and is readable from wherever the next attempt runs. A `resume` retry on the reference runtimes falls back to `fresh_with_feedback` with `strategy_fallback` recorded and shown, `fresh_with_feedback` is the strategy that works on every runtime and the documented default where the previous attempt's work is needed, a fallback attempt's usage records are whole figures, and `test_resume_after_original_environment_removal` is specified in scheduler.md section 7. Exporting the checkpoint before `release` as an artifact version with its retention and the compatible harness version is recorded in ADR-0007 as the post-MVP design.

### A11 An aggregate budget is controlled on aggregate data

<a id="a11-aggregate-budget"></a>

**Priority P1. Status: missing mechanism; budgets are already correctly called soft.**

**Basis.** [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md) and the limits table in [ir.md](../specs/ir.md) define run and sub-graph budgets, and the runner is named the enforcer. The runner observes its own session; the scheduler also checks sub-graph budgets, but the division of authority and the delivery of the aggregate remainder are undefined.

**Counterexample.** The run budget is 10. Two sessions report 6 each. Neither reached 10 locally, and the run has spent 12. The next iteration may forget the previous cost if it receives only the original budget.

**Proposed change.** For MVP the runner controls the local session limits; the control plane aggregates past and current spend, stops admission and, under the chosen policy, cancels active attempts. The alternative, handing attempts reservations of the shared budget, is more complex but limits parallel overshoot better. Define usage freshness, the semantics of `partial` and `unavailable`, the action on a delayed heartbeat, the summation of previous sub-graph iterations and the difference between estimate and actual billing. Keep the separate rule for the namespace daily budget: it blocks new attempts, running ones finish. **Hard timeout.** If hard means the computation stops even under network isolation, the session runtime must receive the deadline and enforce it locally; if only the control plane's transition to timeout is guaranteed, the promise is weaker. Delivering a cancel through the heartbeat does not prove that remote work stops during a partition; an explicit clock model is needed.

**Acceptance.** `test_parallel_attempts_share_run_budget`, `test_subgraph_budget_includes_previous_iterations`, `test_resumed_session_does_not_double_count_usage`, with controlled usage events proving the documented action at the threshold, incomplete data included; the console shows the freshness of the estimate and the reason for a stop. `test_runtime_enforces_deadline_during_control_plane_partition`.

**Files.** ADR-0010, limit ownership, workload and control messages, cost views. Target: 6 to 9, without promising an absolute monetary hard cap under imprecise metering.

**Disposition.** Accepted in the milder form, the same rule as the namespace daily budget: the scheduler's limits pass sums committed usage records and live counters per run and per sub-graph instance; when a scope is over its budget plus headroom, no further attempt is admitted in it (a ready instance there fails with `budget_exhausted`) and running attempts finish and keep their outputs, since killing a nearly finished session over an aggregate is worse than the overshoot of what is already running. The node deadline travels in the workload and the runner enforces it locally during a partition. Applied by [#13](https://github.com/realkarych/kora/issues/13).

### A12 Reserve shared capacity atomically and account for provider quotas

<a id="a12-capacity-reservations"></a>

**Priority P1 for the reservation semantics; P2 for tests with several schedulers. Status: missing contract and a conditional high-availability risk.**

**Basis.** The scheduler passes run under a per-run lock. Admission counts the global, namespace, graph, runtime and fan-out limits. Restart and high availability keep one scheduler in MVP but design for several.

**Scenario.** On PostgreSQL two schedulers hold different run locks, both see one free global slot and both create an attempt; the run lock does not protect the shared resource. Even with one scheduler it must be defined whether `created` and `claimed` attempts count as occupied reservations, otherwise queued work may go uncounted.

**Proposed change.** Choose an atomic reservation per scope: a conditional counter update, a scope lock with a defined acquisition order, or another provable transactional mechanism; define the acquire and release transitions and the states that occupy capacity. No fairness system in the first slice; a correct semaphore. **Additional hypothesis.** Different runtimes and namespaces may use one provider account or credential pool; limiting containers does not prevent saturating a shared quota, and admission has no identity for that resource. Archon already has provider-level concurrency and quota and rate-limit handling, which makes the risk practical. Add a resource pool per provider, account or model, or another explicitly configured quota; honour `Retry-After` and reset windows on rate limits, with injected randomness for jitter so tests stay deterministic; show queue rules and the reason for waiting to the user.

**Acceptance.** `test_last_slot_reserved_once` on real PostgreSQL with two admission participants; `test_created_attempt_reserves_capacity`; `test_capacity_released_once`; `test_shared_provider_pool_limits_multiple_runtimes`; `test_quota_reset_defers_admission`. SQLite checks the same external semantics. The high-availability test is not a requirement for a two-replica MVP, but the reservation contract must not wait for public stabilisation.

**Files.** Admission states, runtime and provider configuration, scheduler transactions, observability. Target for cost and admission: 6 to 9.

**Disposition.** The definition of occupying states and the atomic reservation statement are accepted and applied by [#13](https://github.com/realkarych/kora/issues/13). The two-scheduler race test is deferred to the high-availability step, since MVP runs one scheduler and says so. The provider quota pool is deferred after MVP; the gap is already recorded as an Archon win in [docs/archon-comparison.md](../../docs/archon-comparison.md).

### A13 Prove the composition of runtime and VCS instead of a matching `remote` label

<a id="a13-remote-composition"></a>

**Priority P1. Status: missing composition contract.**

**Basis.** For the `remote` workspace target, [protocols.md](../specs/protocols.md) section 4 says the VCS calls the runtime's API or returns a bootstrap command the runner executes. But `Vcs.checkout` returns a `WorkspaceRef`, and the runtime interface defines no shared interface for remote workspace operations. The intersection of capabilities on the string `remote` proves agreement on the location kind, not compatibility of transport and operations.

**Risk.** VCS and runtime authors must know each other; instead of M independent runtimes and N VCS, M × N integrations appear, which destroys one of kora's main advantages for organisations.

**Proposed change.** Choose one minimal boundary: (1) the runtime offers standard working-environment operations through which the VCS performs its actions, or (2) the VCS produces a typed checkout and publish plan that the runtime or runner executes in the right environment and returns a typed result. In both cases define where credentials are, who verifies the result and how recovery works. This does not require a shared filesystem or a universal remote shell with unlimited rights. If bootstrap is given to the runner, the contract describes it as a separate preparation phase before the agent session. Keep FR-8.8: an enterprise VCS client may run outside the sandbox; the bootstrap decision must not silently require that client and its credentials inside the sandbox for every provider.

**Acceptance.** `test_vcs_runtime_cross_product`: one git VCS implementation without branches on the runtime name performs checkout, diff, publish and release on two really different runtimes; a second VCS integration works at least with the same two runtimes through the shared contract or declares justified limits. For a runtime without a shared filesystem, artifact transfer and recovery after workspace preparation are checked.

**Files.** ADR-0005, the runtime and VCS DTOs, the compatibility validation `IR009`, conformance. Target for protocols: 8 to 9 after a second integration is proven; the capability flag alone raises nothing.

**Disposition.** Accepted. Applied by [#20](https://github.com/realkarych/kora/issues/20) with the second option: the VCS returns typed `CheckoutPlan` and `PublishPlan` values (files to materialise, commands as argv, credentials as secret references, named captures) and never touches the workspace; the session runner executes them inside the runtime as the preparation phase before the harness starts and the collection phase after the session ends, and reports the `WorkspaceRef` and the `VcsRef` through the normalised event stream. The workspace target kinds and the bootstrap command are gone; runtime and VCS declare `plan_executors` instead, and FR-8.8 is preserved by `worker_host`, under which the worker executes the plans on its own host where the runtime exposes the workspace there. `test_vcs_runtime_cross_product` proves the composition on two runtimes with a VCS that receives no runtime identity; ADR-0005 records the decision and the rejected label match.

### A14 Add a durable target of work above runs

<a id="a14-target-identity"></a>

**Priority P1 before the user-experience structure. Status: product hypothesis with a concrete gap in the model.**

**Basis.** The data model stores runs, node instances, attempts and artifacts; fan-out uses a positional index; the overlap policy applies to the graph. The audience is platform and product teams, not only the author of one run.

**Scenario.** Yesterday the graph prepared a change request for SQL target Q, still waiting for review. Today another trigger picks Q again. Webhook delivery idempotency does not help: the events are different and correct. `allow` can create a competing change; `queue` on the whole graph also delays an independent target R. The history of one target falls apart into runs.

**Hypothesis.** For the primary user, "what is the current state of this target and which change was accepted" matters more than "which run was green". Dagster assets and partitions, Sourcegraph changesets and the GitHub Agentic Workflows work queue show the usefulness of the separation without prescribing kora's model.

**Proposed change.** For MVP an optional stable `target_key` with namespace scope, rules for forming it and its link to the source revision; a projection of the target's history over runs: active work, candidate, open change request, review revision and outcome. No full asset catalogue or campaign framework. Three keys apart: the identity of the inbound delivery, of the business target and of one execution attempt. Concurrency policy per target: join existing work, create a new revision, defer, or deliberately run an alternative strategy; an unconditional ban on two runs per target would be too rigid. A stable mapping key inside a fan-out helps readability and addressing but does not replace global target identity; array order must not become the only user-facing name of a result across runs.

**Acceptance.** `test_same_target_triggers_follow_declared_policy`: two different triggers for Q create no unintended competing change requests; R keeps working in parallel; deliberate comparison of strategies for Q is possible by explicit policy; a reordered input list still maps old targets to new ones, not only indices.

**Files.** Product model, target linkage, admission key, public API, key screens. Target for user experience: 6 to 9, if the pilot confirms the value of the view.

**Disposition.** Decided on 2026-10-16 in [#23](https://github.com/realkarych/kora/issues/23) and recorded as [ADR-0014](../../docs/decisions/0014-target-identity.md), delivered through [#38](https://github.com/realkarych/kora/issues/38): an optional `run.target_key` with namespace scope, bound from a run parameter or from a declared artifact field; a per-target policy `allow` or `defer` as one more admission scope that cancels and closes nothing; the target's history as a read projection over runs, approval requests and the `VcsRef`s they bind; the three keys (delivery, target, attempt) kept apart. `supersede`, `join`, a caller-supplied key and a stable fan-out mapping key are deferred after MVP; fan-out identity stays positional, so the acceptance test drops its reordered-list clause. The attention states and the review-revision page follow in the console design document ([#39](https://github.com/realkarych/kora/issues/39)).

## Corrections to the competitor analysis

Competitors do not prove that kora is unnecessary; they change the cost of implementation and the place where superiority has to be shown. Claims of the form "nobody else has this" must rest on specific semantics and a checked version, or the architecture risks optimising against a gap that has already closed.

### Archon

The subject is coleam00/Archon as a TypeScript/Bun workflow engine, not the earlier Python/RAG image of the project. Checked release: v0.11.1, commit `aa095446cf1915901e777433e043b8b8b77dfbe1`. Simplified shape: workflow description, DAG executor, agent and script executions in git worktrees, persisted execution events and artifacts, console and integrations. A close subject-matter competitor, not only a manager of terminal sessions; no conclusion about all of its distributed guarantees follows from the fragments read.

Confirmed in the sources: the execution schema carries separate invocation and attempt identities including an attempt id and `startedAt`; execution construction creates an invocation UUID and an attempt UUID separately; the artifact schema describes iteration-qualified outputs with `loopGroupPath` and run and session identity; the DAG executor restores earlier iterations' outputs from stored completion events, so updating an in-memory previous-output map does not mean the whole history is absent.

**Correction for kora.** The statements in [positioning.md](../../docs/positioning.md) that only kora has attempts as objects and that Archon keeps only the last output are too strong. The more precise difference is kora's proposed queryable causal lineage from the consumed input version to the output, the single semantics of sub-graph retry and distributed recovery, evidence-bound approval and infrastructure portability. Archon's attempt id does not prove equivalence on all of these, but it rules out a flat denial of an attempt model. **Take:** a clear complete user process, runnable workflow packs, provider limit handling and diagnostic convenience. **Prove for kora:** the same useful graph on a second runtime and VCS pair with every guarantee kept, not another successful graph on git and Docker.

### GitHub Agentic Workflows

The official work-queue documentation describes an immutable work DAG, separate claims for attempts, reservations, scheduling and retry policies, completion and a verifiable delivery result; the architecture separates the agent's staged write intentions from trusted execution of safe outputs; orchestrator patterns describe workflow composition. Sources checked at release v0.92.0, commit `a68f5a9436d2a014dbd1d1d48ff86c4bdec7424a`: the work definition and claim operation types, scheduling with capacity and retry, and the compilation of a separate output job.

**Correction for kora.** Treating it as "one agent job without orchestration" is no longer tenable. In DAG, attempts, scheduling and controlled publication it is a direct architectural reference point, while its binding to GitHub, git and Actions leaves room for an independent platform with other VCS and runtimes. **Take:** the separation of the proposed change from the authorised publisher, work apart from an execution claim, an uncertain-result submission state; useful for A01, A08 and A14.

### Sourcegraph Agentic Batch Changes

By the official product documentation, the user works with persistent batches and changesets across repositories and code hosts, with canary and gradual rollout, updating of existing changesets and handling of CI and conflicts; Claude, Codex and deterministic scripts are supported; execution uses isolated containers and publication is performed by Sourcegraph components with separate authority. These are documented properties, not conclusions from an audit of a closed implementation. A strong competitor in how the user's work is organised, even if it does not replace an arbitrary portable graph engine: platform value does not begin and end with the scheduler; the state of each change, re-work on it and human intervention matter.

### Open SWE and LangGraph

Open SWE was checked on the `main` branch at commit `e098d66aae97cec5b55184b0393b4310e31a492b`, without claiming that every property is in a release. Its lifecycle distinguishes a missing from an unreachable sandbox; its handoff carries the branch and working changes; its voting checks the fingerprint of the shown diff before review; its merge logic lets approvals survive in some cases on the agent's explanation, which is weaker than kora's strict immutable binding. Lessons: unavailability differs from absence, a workspace handoff has explicit content, an approval needs a fingerprint. The generalisation "no stage gates" needs refinement; kora's authored graph semantics remain a separate advantage.

LangGraph allows arbitrary Python nodes and durable checkpoints and interrupts and does not require each model call to be a graph node, so the LangGraph contrast in [positioning.md](../../docs/positioning.md) is too categorical; its difference is the library level, at which the coding-workspace lifecycle, VCS, evidence and platform functions have to be assembled by the user.

### A testable hypothesis of kora's difference

Proposal: kora gives the same managed process of change and verification across harnesses, runtimes, VCS and an organisation's internal tools, keeping the causal history of attempts, evidence and human decisions. Testing it is stronger than a feature matrix: (1) a real recurring kind of engineering work with a stable target; (2) the process runs from candidate to verified result and human decision; (3) the same semantics work on a second infrastructure pair without special branches in the scheduler; (4) a crash, a reviewer's rejection and a new push neither destroy evidence nor silently repeat an external action; (5) the platform team can explain the state and recover work through the public API and console. The external-demand hypothesis stays open even after technical success: preference for a new control plane over a ready competitor has to be tested with platform-scale users.

**Disposition.** The factual corrections to the engine analysis and the Archon claims were applied by [#22](https://github.com/realkarych/kora/issues/22) on 2026-10-16: the Archon loop claim was verified against the cited files at v0.11.1 and narrowed to "an earlier attempt is not addressable as an input" in [docs/positioning.md](../../docs/positioning.md), [docs/archon-comparison.md](../../docs/archon-comparison.md) and the [sourced comparison](../research/agentic-pipeline-orchestrators/archon-vs-kora.md); the dated DBOS note is [dbos.md](../analysis/own-scheduler-vs-existing-engine/dbos.md).

## How mature orchestrators are built, and the applicable lessons

None of the rows means the product is a better core for kora; execution boundaries, state and operational mechanisms are compared.

| system | execution shape | take for kora | limit of the analogy |
|---|---|---|---|
| Dagster | Python definitions, a separate code location, webserver and daemon, run coordinator, run launcher, run worker, executor and ops; instance storage holds execution metadata | separate code loading, admission, infrastructure launch and execution; navigation by durable results | assets give no VCS review, accepted change or sum of the cost of all rejected candidates by themselves |
| Airflow 3 | DAG processor, serialised DAG in the metadata database, scheduler and executor, worker supervisor and task process; the worker uses the execution API | isolated code processing, an API boundary, durable waiting instead of a busy process | the familiar task-retry model does not coincide with kora's sub-graph and evidence semantics |
| Prefect | deployment and API, work pool and queue, polling worker, flow process, task runner, tasks; other deployment modes exist | platform templates for environments, a clean split between code and deployment metadata | run-time Python flexibility differs from a pre-compiled document; pause and suspend have different consequences |
| Kestra | YAML flow and API, queue and control roles, worker controller and workers, task plugins; the JDBC backend may use SQL for queue and repository | an explicit state machine and isolation of the worker channel; standalone does not have to mean different semantics | the current documentation is not the Kestra 2.0 release checked in the earlier research |
| Temporal | client, history service and task queues, polling workers; workflow replay, external actions in activities | durable waits and signals, crash recovery, version routing | an activity may repeat; idempotency and reconciliation of an external action stay with the application |
| DBOS | a library inside the application, SQL checkpoints and durable queues; no separate orchestration server required | check whether durability can be had while keeping a small SQL-based control plane | semantic compatibility and coordinated recovery are needed; it is not a ready kora interpreter |

**Dagster.** Most useful for its view of the result object across runs: partitions and backfills answer questions about selected parts of the data; for kora the analogue is often a set of targets at given source revisions. A dynamic mapping key identifies elements inside a run but is not a global business key. A shared good decision: a hash of code and inputs does not replace the digest of an actual non-deterministic output.

**Airflow.** The engine analysis in [verdict.md](../analysis/own-scheduler-vs-existing-engine/verdict.md) records that the scheduler consumes Python DAG files and that serialised JSON cannot serve as an intermediate representation. That misdescribes current server-side execution: DAG serialisation has long been used by the scheduler, and the documentation describes a versioned SDK and server contract from 3.1, while noting that the claimed independence is not yet fully implemented. Correcting the argument is not a recommendation to build kora on Airflow. For durable waits Airflow has deferral and triggerer machinery; the lesson, that waiting for a person or an event must survive process restarts and occupy no session slot, is already in kora's design and needs race and authority checks.

**Prefect.** A human-in-the-loop checkbox hides different models: pause keeps the execution waiting, suspend ends it and needs conditions to reschedule. For kora the distinction between "no active runtime" and "a saved context that can be continued" must be explicit.

**Kestra.** The current unversioned architecture page describes six roles including a worker controller; a worker opens an outbound bidirectional gRPC connection and never touches the queue or database; in standalone the roles can run as threads of one process. These facts must not be ascribed to the older version in kora's analysis; version, edition and secure-transport requirements are checked at prototyping time. Kestra also has the word attempt; kora's exact difference lies in the combination of lineage, feedback inputs and review semantics.

**Temporal.** A strong example of execution recovery and versioning. But "Temporal gives native at-most-once runtime submission" is a wrong shorthand: a lost activity completion allows a repeat, so a remote runtime still needs a durable key and an authoritative lookup. For long-lived graphs a proven routing strategy to a compatible deployment version is useful.

**DBOS.** A materially important omission from the original comparison. The official Python guide uses SQLite by default and recommends PostgreSQL for production; the library embeds in the application. This is close to kora's chosen shape and weakens the argument "either own durability or a separate large infrastructure". Distributed recovery needs separate verification; without the vendor's coordination service, recovery coordination stays with the application. DBOS executors with database access must stay inside the control plane if kora's outbound-only boundary for external workers is kept.

**Where the ETL analogy ends.** Successful completion of an agent session does not prove the result is useful. A retry may create a new variant of code and new external effects; feedback becomes the input of the next attempt; a person approves specific evidence. kora therefore needs its own domain model regardless of engine; it does not follow that every durable-execution mechanism must be implemented in-house.

## How to decide about the own scheduler

**Recommendation: keep kora's control plane; treat the own scheduler as the working option until a limited prototype.** There is no sufficient ground to rewrite the project on Temporal, Airflow or Dagster now. There is ground to stop treating the engine choice as finally proven.

[ADR-0001](../../docs/decisions/0001-own-control-plane.md) states openly that the analysis was a desk analysis without a prototype. Its evidence base needs these corrections: native at-most-once submission attributed to Temporal; the opposition of Airflow to a serialised representation; the categorical absence of attempt objects in other engines; the absence of a production standalone mode at competitors used as a decisive argument while kora's own standalone mode is defined as a development convenience; the claimed necessity of two independent sources of truth under Temporal, where history could be authoritative and SQL a projection (still requiring reconciliation, but not two masters by definition); and the promise of an easy future backend swap on the ground that state is JSON-compatible and passes are stateless, when the transactional boundary of state, approvals and artifacts would change anyway.

### A minimal comparative prototype

Do not build three platforms. Implement one fixed scenario on the minimal SQL scheduler and on **one** alternative. Check DBOS first as the closest to the deployment shape; choose Temporal for comparison if operational requirements admit its service and the main unknown is mature distributed durability. The shared scenario: typed inputs, an agent attempt, a deterministic gate, a fan-out of two candidates, a rejection with feedback, a new sub-graph iteration, a content-bound approval, a durable timer, a controlled publish. Fake harness and runtime through the real protocols, plus a few critical cases with a real local or Docker process; the product frontend is not needed.

| check | condition every variant must meet |
|---|---|
| Lost response after submit | no second physical session for one attempt id; the authoritative handle is recovered |
| Unreachable runtime | unknown does not become absent or a licence for an uncontrolled repeat |
| Late upload | a revoked attempt does not make its output current and does not complete the new instance |
| Long wait | approval and timer occupy no runtime slot; a restart keeps the inputs, the deadline and the single authorisation |
| New code revision | old approval and evidence do not open downstream for the changed candidate |
| Retry and lineage | the new attempt consumes exact versions; old history and cost are kept |
| Admission | capacity is not exceeded through a race or uncounted queued reservations |
| External effect | a lost publish response is resolved by reconciliation or a visible unresolved state |
| Upgrade | an active workflow continues on compatible code or is blocked by a predefined rule |
| Operational cost | deploy, credentials and routes, backup and restore, recovery and upgrades are described; the mechanisms that remain in-house are visible |

For the SQL path PostgreSQL and SQLite are mandatory, and for high availability real PostgreSQL contention. For DBOS, prove separately who takes over the execution of a dead control-plane executor. For Temporal, choose explicitly the owner of transitions and the way the SQL projection is recovered; never let two components take one decision independently. Measure performance against the accepted non-functional requirements, not an invented throughput contest: long coding sessions make correctness, operational cost and clarity of recovery more important than microseconds of a scheduling pass.

**Selection rule.** First the mandatory invariants and deployment constraints; then the volume of own transition and recovery logic, the complexity of upgrades and diagnostics. Keep SQL if it is simpler on the proven scenarios; choose a library or engine if it really removes substantial own machinery. A wrapper inside which almost the whole previous scheduler remains gives no expected benefit. A production `ExecutionBackend` interface for two experimental implementations is not needed yet; explicit semantic boundaries and a verifiable prototype are. Restate and other engines may be studied further; nothing in this critique justifies recommending them without an equally concrete check.

**Disposition.** The factual corrections were applied by [#22](https://github.com/realkarych/kora/issues/22) on 2026-10-16 in [verdict.md](../analysis/own-scheduler-vs-existing-engine/verdict.md) and in [ADR-0001](../../docs/decisions/0001-own-control-plane.md), which carries an amendment dated 2026-10-16. The comparative prototype is not started before the first vertical slice: the slice is the prototype of the SQL scheduler, and the ten checks above are its acceptance list. The DBOS comparison is re-evaluated when the slice has data.

## Proposed target model of authority and evidence

So that the findings are not fixed one by one with unrelated flags, a few cross-cutting invariants are worth agreeing on. This is a logical model; it does not require a table per row.

| concept | question it answers | main rule |
|---|---|---|
| Target | which durable goal is the work for? | not mixed with the delivery id or the run number; competing changes follow an explicit policy |
| Run and execution definition | which process was chosen for this work? | the graph version and the meaning of the execution never change silently after the fact |
| Node instance and attempt | which attempt executes the step? | one current authority over current state; past attempts are kept |
| Execution domain and lease | where does the work run and who may observe and control it? | lookup refers to the right domain; a change of owner is not a repeated start |
| Artifact version and consumed inputs | which actual data was produced and from what? | versions are immutable; the link from an attempt to its inputs is recoverable after invalidation |
| Review revision | which code and which evidence were shown to the person? | an immutable set of candidate, evaluator, input, result and policy references |
| Approval decision | who authorised the next step and for what? | applies only to the stated review revision and is checked at use |
| Effect operation | which external result did the platform intend to create? | an identity before the call, an uncertainty state and a reconciliation procedure |
| Checkpoint and workspace snapshot | what can really be continued after release? | handle, transcript and portable bytes do not substitute for one another |

Example chain in the SQL scenario: `target → run with graph version V1 → attempt A with inputs I → candidate C → evaluator E over C and data D → evidence R → review revision (C, E, D, R) → approval → publish operation → observed result`. At every transition it must be clear who writes the authoritative state, which versions it checks and what happens if the confirmation is lost. For an asynchronous action, the admissible delay, the behaviour under unavailability and the guarantees until confirmation are defined. Delivering a cancel through the heartbeat is acceptable; revoking the right to publish a current output must happen before that delivery completes.

### Invariants for technical review

1. **Immutable history.** For any completed attempt the original document, the resolved configuration, the input versions and the provenance of the result are recoverable.
2. **One current result.** Cancellation or supersession immediately revokes the old attempt's right to change current state, regardless of signal delivery to the runtime.
3. **Accounting apart from authority.** Losing the right to complete a step does not forbid safely recording its actual cost and historical data.
4. **Honest uncertainty.** A timeout of an external API does not automatically become "the action did not happen".
5. **One revision per human decision.** Code, checks and decision refer to one set of immutable inputs.
6. **Verifiable security.** A required policy is either enforced by concrete mechanisms or the run is rejected.
7. **Independence of verification.** A candidate cannot assign itself a trusted verdict.
8. **Isolation of user code.** Compile and execution do not inherit the authority of the API or the control plane.
9. **Correct capacity.** Creating the next attempt reserves the resource; losing a worker creates no uncontrolled new reservation.
10. **The useful result is addressable.** A user can find the current state of a target without reconstructing it by hand from several runs.

## Priorities for the user-experience design

The full console is already decided (FR-7.3); this is not a proposal to cancel it. The recommendation concerns the order of design: first the entities, the decisions and the failure states, then the visual form and the scaling of the screen set.

- **Attention queue.** Pending review, stale evidence, exhausted budget, unknown publish outcome, lost runtime, incompatible resume. Each state explains what is known, what the system does not know and which permitted action changes the situation; not everything collapses into one failed.
- **Target page.** Current candidate or change request, source revision, previous runs, the accepted decision and the measured result; compare not only run speed but the cost of the whole search for the accepted change, rejected attempts included; a result not yet verified after rollout is shown as a separate fact, a merge is not an automatically confirmed effect.
- **Review-revision page.** Side by side: the specific diff, the required checks, the evaluator version and measurement data, cost completeness, the narrative and the provenance of facts. On a new push it is clear which revision is stale and what must be recomputed. The approve button acts on an explicit revision, never on an implicit latest head.
- **Run graph.** The causal history of execution and its diagnostics; a node shows instance, attempts and superseded against current outputs apart. Many green nodes do not replace the state of the target's result.
- **Attempt comparison.** Differences in inputs, configuration, candidate diff, checks and cost; "resume" and "new session with feedback" are distinguished, as are "new graph version" and "retry of the old".
- **Product acceptance.** In the pilot an engineer without database access explains why a change is ready for approval, why an old approval lapsed, where the work is now and why it waits; an operator resolves an ambiguous publish and a lost worker through the public surface. These scenarios are prepared before all console pages are detailed.

**Disposition.** Carried into the console design through [#23](https://github.com/realkarych/kora/issues/23).

## Order of fixes

The critique proposes seven packages; the owner regrouped them into the issues below so that each pull request closes one issue. The schema of the execution tables and the public execution API are better fixed after the trust, identity, revision and effect packages; the harness-mechanism research and the target-identity design can run in parallel; the visual system of the console can start earlier, but its final information architecture depends on target, revision, evidence and recovery states.

| order | package | issue | findings |
|---:|---|---|---|
| 1 | Specification contradictions with an answer in the text | [#13](https://github.com/realkarych/kora/issues/13) | A06, A07, A11, A12 |
| 2 | Trust boundaries | [#14](https://github.com/realkarych/kora/issues/14), [#15](https://github.com/realkarych/kora/issues/15) | A01, A02 |
| 3 | Review revisions and deterministic selection | [#16](https://github.com/realkarych/kora/issues/16), [#17](https://github.com/realkarych/kora/issues/17) | A03, A05 |
| 4 | Versions, resume and remote workspaces | [#18](https://github.com/realkarych/kora/issues/18), [#19](https://github.com/realkarych/kora/issues/19), [#20](https://github.com/realkarych/kora/issues/20) | A09, A10, A13 |
| 5 | Small contracts without decisions | [#21](https://github.com/realkarych/kora/issues/21) | A04, A08 |
| 6 | Engine analysis and competitor facts | [#22](https://github.com/realkarych/kora/issues/22) | corrections above |
| 7 | Console design backlog | [#23](https://github.com/realkarych/kora/issues/23) | A14 and the user-experience priorities |

Agreed document changes: ADR-0001 and the engine research (facts corrected; the product control plane apart from the execution mechanism); ADR-0003 and ADR-0007 (ownership transfer, execution domain, clear-downstream, graph-version retry); ADR-0008 and ADR-0009 (immutable review revision, actual security capabilities, evaluator independence, the compiler boundary); ADR-0010 (local and aggregate budget, deadlines, reservation and admission); ADR-0012 (deterministic packing and selection through the existing command node); `data-model.md` (immutable bindings, publication authority, effect identity, target linkage, checkpoint references); `scheduler.md` (exact transitions and guards, adoption through the permitted channel, retry and versioning, capacity and budgets); `protocols.md` (capabilities, execution domain, VCS recovery, remote workspace composition, checkpoint semantics); the graph document, the DSL, the API and the canonical examples (one reflected semantics, machine-checkable errors, no stale examples); positioning and competitor research (presence of a primitive distinguished from completeness of the needed semantics; volatile facts with a check date in research); requirements and user experience (updated only where promised behaviour changes).

## Evidence that raises the architectural score

The minimal evidence for re-assessing the design without verbal explanations.

| group | required observable evidence |
|---|---|
| Policy | every promised combination is supported by a real mechanism or rejected; the forbidden effect does not happen |
| Compile | uploaded Python reads no API secrets or data, does not run with its authority and is resource-limited |
| Evidence | A and B with different behaviour do not share unsuitable gate results; a trusted verdict cannot be substituted by the candidate |
| Determinism | selection by formula, packing and artifact-reference checks need no model and are reproducible |
| Fencing | clear covers `created`, `claimed`, `submitted`, `running` and `finishing`; a late attempt does not restore current state |
| Recovery | a crash between submit and handle persistence starts no second physical session; the right domain is found or uncertainty is kept |
| Effects | a crash after creating a change request or comment leads to no blind repeat; the outcome is reconciled or explicitly unresolved |
| Versions | adding a mandatory gate in a new graph cannot be bypassed through a retry of an old run |
| Resume | after the original environment is removed, a real resume or an honest fallback with preserved changes is proven |
| Limits | aggregate spend includes parallel and past attempts; the chosen reaction happens at a known data freshness |
| Capacity | the shared last slot is reserved once; a quota reset creates no retry storm |
| Protocols | a second infrastructure pair runs the same graph without special branches in the scheduler or manual bypass of contracts |
| Target UX | two events for one target follow the declared policy; independent targets are not blocked; the result history is readable |
| Upgrade and restore | backup, restore and upgrade preserve evidence references, active waits and the recoverability of executions |

Database checks run on PostgreSQL and SQLite; synchronisation through controlled events, barriers and deadlines; time and randomness injected; real components and fakes through the protocols; no monkeypatching, hidden transport substitution or arbitrary sleeps; no real models or code hosts in mandatory CI, as [AGENTS.md](../../AGENTS.md) and NFR-10.1 require. An experiment with a specific harness that needs a real model session records version, configuration, the observed hook or effect and the output limit separately; a passed smoke test is useful additional evidence, not a replacement for the deterministic suite nor proof for future versions.

### Re-scoring scale

- **7 to 8:** contradictions removed, clear contracts adopted, fault scenarios not yet proven.
- **8 to 9:** the minimal engine passes the key fault scenarios, real harness capabilities confirmed, compile and evaluator isolated.
- **9 to 9.5:** a second runtime and VCS pair keeps the semantics, the pilot closes the full result lifecycle, recovery and upgrade work by the described procedures, limits are clear to the user.
- **Approaching 10:** the claimed working scale confirmed, maintenance cost acceptable, several independent scenarios and integrations need no model change, operational incidents fit the stated contracts.

The score does not grow with the number of tables, protocols or documentation pages; it grows when an important property becomes easier to explain, verify and recover after failure.

## What does not need to be redone

There is no ground to change the whole stack, abandon the API-first principle, build a visual graph editor before the first useful process, introduce a universal transform language or copy Dagster's full asset catalogue. Prototypes need not become three backend products, and an outbox is not needed for every event regardless of its meaning. The priority of root changes is limited to **execution authority, evidence trustworthiness, immutable revisions, the recovery domain, reconciliation of external effects and a durable object of the user's work**: the parts that are expensive to add after real data and integrations exist. The rest develops from the results of the first complete scenario.

## Sources and the limits of their use

References into kora were checked at commit `d049b2b4948ff58c4651db009ab17cb02be7893d`; this record links the current files. Competitor sources, by level of confirmation, all checked on 2026-10-16:

- **Archon v0.11.1:** the named files of the release commit were read; the system was not run.
- **GitHub Agentic Workflows v0.92.0:** the listed sources were read; broader capabilities are described from the current official documentation and not ascribed to the release.
- **Open SWE:** a pinned commit of the `main` branch, not a release.
- **Sourcegraph:** official documentation; closed internal implementations were not checked.
- **Airflow, Dagster, Prefect, Kestra, Temporal, DBOS and the harness protocols:** primary documentation; the applicability of their mechanisms to kora is an inference or a hypothesis, marked as such in the sections above.

External pages:

- Agent Client Protocol, tool calls: <https://agentclientprotocol.com/protocol/v1/tool-calls>
- Airflow: overview <https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html>, DAG serialisation <https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/dag-serialization.html>, deferring <https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/deferring.html>
- Archon v0.11.1 files: `packages/workflows/src/schemas/node-execution.ts`, `packages/workflows/src/schemas/node-artifact.ts`, `packages/workflows/src/node-execution.ts`, `packages/workflows/src/dag-executor.ts`, `packages/workflows/src/executor-shared.ts`, `packages/core/src/config/provider-concurrency.ts` at <https://github.com/coleam00/Archon/tree/aa095446cf1915901e777433e043b8b8b77dfbe1>
- Claude Agent SDK hooks: <https://code.claude.com/docs/en/agent-sdk/hooks>
- Codex app-server: <https://learn.chatgpt.com/docs/app-server>
- Dagster: deployment architecture <https://dagster.io/docs/deployment/oss/oss-deployment-architecture>, dynamic mapping <https://dagster.io/docs/api/dagster/dynamic>, backfills <https://dagster.io/docs/guides/build/partitions-and-backfills/backfilling-data>, versioning <https://dagster.io/docs/guides/build/assets/asset-versioning-and-caching>
- DBOS: architecture <https://docs.dbos.dev/architecture>, Python guide <https://docs.dbos.dev/python/programming-guide>, recovery <https://docs.dbos.dev/production/workflow-recovery>
- GitHub Agentic Workflows: work queue <https://github.github.com/gh-aw/reference/work-queue/>, architecture <https://github.github.com/gh-aw/introduction/architecture/>, orchestrator patterns <https://github.github.com/gh-aw/patterns/orchestrator-ops/>; files `pkg/workqueue/types.go`, `actions/setup/js/work_queue_scheduler.cjs`, `pkg/workflow/compiler_safe_outputs_job.go` at <https://github.com/github/gh-aw/tree/a68f5a9436d2a014dbd1d1d48ff86c4bdec7424a>
- Kestra: architecture <https://kestra.io/docs/architecture>, retries <https://kestra.io/docs/workflow-components/retries>
- LangGraph: graph API <https://docs.langchain.com/oss/python/langgraph/graph-api>, interrupts <https://docs.langchain.com/oss/python/langgraph/interrupts>
- Open SWE files: `openswe/sandboxes/lifecycle.py`, `openswe/sandboxes/handoff.py`, `openswe/expedited_review/voting.py`, `openswe/expedited_review/merge.py` at <https://github.com/langchain-ai/open-swe/tree/e098d66aae97cec5b55184b0393b4310e31a492b>
- Prefect: work pools <https://docs.prefect.io/v3/concepts/work-pools>, deployments <https://docs.prefect.io/v3/concepts/deployments>, task runners <https://docs.prefect.io/v3/concepts/task-runners>, flow-run API <https://docs.prefect.io/v3/api-ref/python/prefect-flow_runs>
- Sourcegraph Agentic Batch Changes: <https://sourcegraph.com/docs/agentic-batch-changes>, configuration <https://sourcegraph.com/docs/agentic-batch-changes/configuration>
- Temporal: workflow execution <https://docs.temporal.io/workflow-execution>, activity definition <https://docs.temporal.io/activity-definition>, worker versioning <https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning>

The critique does not claim that the proposed tests exist or pass. It changed nothing in the project; this record and the issues it names are the only artefacts.
