# Implementation plan

Scope and implementation plan approved by the project owner under [issue #127](https://github.com/realkarych/kora/issues/127) on 2026-10-19; wording pending line-by-line owner review.

This page orders the work of the first implementation slice so that two developers can work in parallel with little overlap. It records no decisions of its own. The decisions are in [decisions/](decisions/README.md), and the contracts are in the [specifications](../knowledge/README.md).

Where a contract is still open, the page names the issue that settles it, marked *pending*, and the merged text that governs until that issue lands. It does not restate what a pending issue proposes.

The page's own content is the order of work: when each shared contract freezes, which side owns it, and the work items in order. The page is updated in three cases:

- a freeze step merges;
- an issue it cites lands; each design issue opened for this plan (#134 to #142) carries the update of its row, its named contracts (section 7) and its threat-model evidence (section 8) as an acceptance criterion;
- the owner changes the order.

## 1. Slice 1

**Scope.** The [ADR-0001 amendment of 2026-10-19](decisions/0001-own-control-plane.md) defines the slice:

- `kora standalone` on SQLite as the only deployment it ships;
- one agent node on the Claude Agent SDK and one command node on the local runtime;
- a manual trigger, a git `VcsRef` output and an approval that a person answers through the CLI;
- every integration test that touches the metadata database runs on PostgreSQL and on SQLite;
- a restarted worker adopts a live session;
- every API operation the slice adds meets the parity rule of [ADR-0002](decisions/0002-ai-first-api-and-thin-clients.md).

[DD-36](../knowledge/reviews/design-decisions-2026-10-15.md#dd-36-implementation-default-ownership) assigns the owners of its defaults.

[Issue #140](https://github.com/realkarych/kora/issues/140) (pending) settles the boundaries of the slice:

- what it refuses at deploy time, and how;
- which operations and MCP transports it ships;
- which harness, code-host and credential contracts it carries.

Until #140 lands, the ADR-0001 amendment and the [roadmap](roadmap.md#what-the-documents-cannot-settle) govern.

**What it proves.** It proves items 1 to 3 of "What the documents cannot settle" in the roadmap, to the extent the slice contains them:

- the lifecycle behaviour of [ADR-0016](decisions/0016-trusted-execution-and-publication.md) and [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md) on the local runtime, in a trusted environment, with no claim of OS isolation;
- the Claude Agent SDK paths of the `claude` adapter;
- the CEL binding, through `approve_when` ([ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates));
- the fake harness and fake runtime passing the same conformance suites as real implementations. Fakes prove core decisions, composition and recovery only, with no OS isolation or real harness interception claim ([threat model](threat-model.md#deployment-and-runtime-applicability)).

**How it is tested without model calls.**

- Integration tests run on the real API, scheduler and worker, with the fake harness and fake runtime (NFR-10.1 and the [testing rules](../AGENTS.md#testing)).
- The `claude` adapter is tested through the evidence layers of [protocols.md section 13](../knowledge/specs/protocols.md#claude-adapter-contracts-issue-125):
  - replay, which proves normalisation, not interception;
  - scripted model, the deterministic driver for interception, not yet observed to work with the tested CLI. What the slice does if it cannot carry the policy cases is settled in #140 (pending);
  - connect;
  - manual, outside CI, which records the real-session, provider-limit and internal-retry traces.

  The connect and scripted-model layers run on the Linux job without an external network.
- Git runs for real against local repositories.
- `tick` runs with an injected clock ([issue #103](https://github.com/realkarych/kora/issues/103), pending).
- PostgreSQL runs through containers on the Linux job, and SQLite runs everywhere.
- A slice-1 graph example is checked by the IR example job.

**What it does not owe.**

- The ten checks of the [scheduler-prototype milestone](../knowledge/specs/scheduler.md#18-scheduler-prototype-milestone-acceptance) belong to roadmap step 5.
- The MVP acceptance on a second runtime and VCS pair belongs to roadmap step 6.
- The Docker and protected-runtime evidence that roadmap item 1 requires is needed before a protected runtime ships.

The component gates of the specifications stay with the slice that implements each component, as the ADR-0001 amendment states. Further exclusions are settled in #140 (pending).

## 2. Contract-freeze order

A step is frozen when its specification text is merged. The work item in the last column then lands the step's typed models, in the package that [issue #104](https://github.com/realkarych/kora/issues/104) assigns. After that, a change goes through a pull request of the owning side, and the other side reviews it. In the work items of section 6, "step N" means the merged specification text of that step.

| Step | Contract | Settled in | Owning side | Models land in |
|---|---|---|---|---|
| 1 | Package layers and the import-layer test, including the placement of shared protocol types, logging and the CEL packages | [#104](https://github.com/realkarych/kora/issues/104) (pending) | server | #104 itself |
| 2 | Shared transition functions, `tick` and the lock order, including how a claim takes the run lock | [#103](https://github.com/realkarych/kora/issues/103) (pending) | server | work item 9 |
| 3 | Run outcome | [#102](https://github.com/realkarych/kora/issues/102) (pending) | server | work item 9 |
| 4 | Built-in artifact types, revision 1, and the names of implicit outputs | [#138](https://github.com/realkarych/kora/issues/138), [#137](https://github.com/realkarych/kora/issues/137) (pending) | server | work item 2 |
| 5a | Runtime, harness, VCS, policy and secrets models, the transfer channel, the harness launcher and its directories, command inputs | [#123](https://github.com/realkarych/kora/issues/123), [#106](https://github.com/realkarych/kora/issues/106), [#134](https://github.com/realkarych/kora/issues/134) (pending); the `claude` adapter text is merged in [protocols.md section 3](../knowledge/specs/protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) | execution | work item 6 |
| 5b | Authentication, artifact storage, artifact type and approval-channel protocols | merged in [protocols.md](../knowledge/specs/protocols.md) sections 5, 7, 8 and 10; section 10 changes under [#135](https://github.com/realkarych/kora/issues/135) and section 8 under [#138](https://github.com/realkarych/kora/issues/138) (pending) | server | work item 8 |
| 6 | Worker route models and the attempt's terminal report | [#139](https://github.com/realkarych/kora/issues/139) (pending) | execution | work item 17 |
| 7 | Error model and route authorization | [#135](https://github.com/realkarych/kora/issues/135) (pending) | server | work item 11 |
| 8 | Fake harness and fake runtime contract, including what a serving process may select | [#136](https://github.com/realkarych/kora/issues/136) (pending) | execution | work item 7 |

Step 7 merges before any route is implemented, including the worker routes of step 6, and before the validator's diagnostics.

## 3. Gaps found before implementation

On 2026-10-19 a check of the specifications against slice 1 found the gaps below. Until each issue lands, the cited text governs and the dependent work items wait.

| Gap | Settled in |
|---|---|
| A successful attempt cannot record runtime quiescence and dispatcher closure before its terminal status, as [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md) requires. A failed command gate cannot reach `command_failed`, and a failure before submit has no transition | [#139](https://github.com/realkarych/kora/issues/139) and [#123](https://github.com/realkarych/kora/issues/123) (pending) |
| A claim through `SELECT … FOR UPDATE SKIP LOCKED` locks the attempt row before the run lock, against the lock order of [scheduler.md section 6](../knowledge/specs/scheduler.md#6-concurrency-limits-fr-610) | [#103](https://github.com/realkarych/kora/issues/103) (pending) |
| The built-in types `VcsRef`, `CommandResult`, `AgentSession` and `StructuredOutput` have no published schemas or digest fields | [#138](https://github.com/realkarych/kora/issues/138) (pending) |
| `approve_when` is optional and has no default | [#137](https://github.com/realkarych/kora/issues/137) (pending) |
| There is no rule for naming outputs | [#137](https://github.com/realkarych/kora/issues/137) (pending), which also records where the rule for the DSL is settled |
| There is no unauthenticated error code and no code-to-status table, and the response envelope carries one error | [#135](https://github.com/realkarych/kora/issues/135) (pending) |
| The committed JSON Schema of the graph is hand-written | Decided by [ADR-0013](decisions/0013-implementation-stack.md): the Pydantic models are the single definition and publish `ir.schema.json`. Work item 2 replaces the hand-written draft, and the IR example job checks the generated file |
| The MCP server's default token lacks `approve`, but standalone issues a single token with both flags | [#141](https://github.com/realkarych/kora/issues/141) (pending) |
| The fake harness and fake runtime have no written contract | [#136](https://github.com/realkarych/kora/issues/136) (pending) |

## 4. Further open questions that slice 1 touches

Rows that name a work item are settled in that work item's issue before its code, as the [open implementation questions](../knowledge/architecture/detailed.md#open-implementation-questions) expect.

| Question | Settled in |
|---|---|
| Price ownership, needed for the run cost on approval requests and the runner's usage relay | [#142](https://github.com/realkarych/kora/issues/142) (pending) |
| Timeout discovery and locking | [#103](https://github.com/realkarych/kora/issues/103) (pending) |
| Shape of client transport failures | [#135](https://github.com/realkarych/kora/issues/135) (pending) |
| Exposure of the unauthenticated health and metrics routes | [#135](https://github.com/realkarych/kora/issues/135) and [#140](https://github.com/realkarych/kora/issues/140) (pending) |
| Evidence that a candidate process group is still the original one before fallback retirement ([owner comment](https://github.com/realkarych/kora/issues/123#issuecomment-6270810525)) | [#123](https://github.com/realkarych/kora/issues/123) (pending), then work item 14 |
| Adoption on Linux when a service manager runs the worker | [#123](https://github.com/realkarych/kora/issues/123) (pending) |
| Health serving (open question "Health serving") | work item 20, through the API process ([api-and-agent-surface.md section 2](../knowledge/specs/api-and-agent-surface.md#2-resources-and-operations), [scheduler.md section 11](../knowledge/specs/scheduler.md#11-metrics-and-health-nfr-73)) |
| Secret-prefix ownership (open question "Secret prefix ownership") | work items 3 and 14, which enforce the existing `IR007` and resolve-time rules |
| Attribution of scheduling-latency intervals | work item 10 |
| Authorization of follow streams | work item 12 |
| Whether the MCP SDK brings its own HTTP stack | work item 11 |
| Bounds for prompt and command templates, rendered when the request is sealed ([ir.md section 7.10](../knowledge/specs/ir.md#710-prompt-templates)) | work item 17, with [#139](https://github.com/realkarych/kora/issues/139) (pending) |

Dry-run placement, skill delivery and the error class of a digest mismatch stay open questions; whether slice 1 needs them is settled in #140 (pending). The remaining open implementation questions do not block slice 1.

## 5. Two sides

| Side | Packages and providers | Shared contracts it owns |
|---|---|---|
| Server | `ir`, the CEL parser, checker and evaluator in the packages #104 assigns, `db`, `engine`, `scheduler`, the public routes of `api` and their `models`, `client`, `cli`, `mcp`, the logging bootstrap; the static-token authenticator, local artifact storage, the CLI approval channel, the artifact-type provider, the observation dispatcher, `kora standalone` | steps 1, 2, 3, 4, 5b, 7 |
| Execution | `protocols` with provider loading; the fake harness and fake runtime; `runner`; the local runtime with the env secrets provider; `executor` with git; the `claude` adapter with the `allow_deny` policy; `worker`, the worker routes, their `models` and their client methods | steps 5a, 6, 8 |

The worker routes are handlers in the API application that call the shared transition functions. The execution side writes them, and the server side reviews every change to a transition function. People take a side as issue assignees.

Every API operation ships in one pull request with its CLI command, its MCP tool and its entry in the parity coverage test ([ADR-0002](decisions/0002-ai-first-api-and-thin-clients.md), [AGENTS.md](../AGENTS.md#code)). Each work item's issue lists:

- the named tests it carries (section 7);
- the threat-model evidence, observability activation rows and measurements assigned to it (section 8).

## 6. Work items

The work items are not issues yet. Each is opened when the owner asks, with the change form, the assignee of its side and checkable acceptance criteria.

| # | Work item | Side | Depends on |
|---|---|---|---|
| 1 | Logging bootstrap and process configuration, with the common logging tests run through the first entry point | server | step 1 |
| 2 | `ir` models of the whole IR and the built-in artifact types of revision 1; the generated `ir.schema.json` replaces the hand-written draft | server | steps 1, 4 |
| 3 | `ir` validator: collected diagnostics, the refusal of features outside the slice per #140, a slice-1 graph example | server | 2, 4; step 7; #140 |
| 4 | CEL parser, checker, projections and evaluation per [ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates), with the packaging, CI and development obligations of the "Expression language" row of the [stack table](../knowledge/architecture/detailed.md#stack-per-component) | server | step 1; 2 for the schema-driven checker |
| 5 | `db`: tables, migrations, repositories and the two-database fixture; later steps add their own migrations | server | 1; step 1 |
| 6 | `protocols` for runtime, harness, VCS, policy and secrets; provider loading; conformance-suite skeletons | execution | 1, 2; step 5a |
| 7 | Fake harness, fake runtime and the directory-backed fake VCS | execution | 6, 13; step 8 |
| 8 | Server providers: static-token authentication, local artifact storage, the CLI approval channel, the artifact-type provider | server | 5, 6; steps 5b, 7; #141 |
| 9 | `engine`: canonical start, admission, approval and cancel transitions, worker-report transitions with the terminal report, completion and reconcile intent | server | 2, 4, 5, 7, 8; steps 2, 3, 6; #142, #123 |
| 10 | `scheduler`: `tick`, the passes the slice needs, lease expiry, inherited-deadline timeouts and wake-up; the scheduler part of the NFR-3.1 measurement | server | 9 |
| 11 | API application and the graph operations; the parity coverage test, the published OpenAPI document, the client contract test and the route-authorization coverage test; the stdio MCP server | server | 3, 8, 9; step 7; #140, #141 |
| 12 | Run operations: trigger, run reads, events, nodes and attempts, logs, artifacts and cancel | server | 7, 11 |
| 13 | `executor` and the git provider, with head reads against a local repository | execution | 6; #140 |
| 14 | `runner` and the local runtime: registry, control channel, journal, transfer, retirement, adoption evidence and the harness launcher; the env secrets provider and redaction | execution | 6, 7, 13; #123, #140, #141 |
| 15 | `claude` adapter and the `allow_deny` policy | execution | 7, 14; #140, #142 |
| 16 | Observation dispatcher: CLI channel delivery and the head reads of VCS-bound approvals | server | 8, 10, 13 |
| 17 | `worker`, the worker routes and their client methods, request sealing with template rendering | execution | 7, 9, 11, 14; steps 6, 7, 8 |
| 18 | Approval operations and reconcile | server | 10, 12, 16; 17 for the end-to-end reconcile cases |
| 19 | Run-read wait ([api-and-agent-surface.md](../knowledge/specs/api-and-agent-surface.md#bounded-run-read-wait-adr-0002)); its process-exit cases run once work item 20 exists | server | 10, 12, 17, 18 |
| 20 | `kora standalone`: composition, token issuance per #141 and health through the API process | server | 17, 18, 19; step 8; #135, #140, #141 |
| 21 | Slice-1 acceptance: end-to-end tests through the CLI and the HTTP API, worker-restart adoption, `test_partitioned_live_host_is_not_assumed_stopped`, the manual `claude` traces and the end-to-end NFR-3.1 measurement | execution | 15, 20 |

## 7. Named contracts by work item

Every executable contract that the specifications name for a slice-1 component belongs to the work items below. A contract, or one leg of it, that needs a component outside the slice is pending until the trigger in the last column lands; the component gates stay with the slice that implements the component.

The ADR-0001 amendment places Docker, Codex, the trigger kinds other than manual, and the node kinds other than agent, command and human input outside slice 1. Every other trigger below follows the slice boundaries settled in [#140](https://github.com/realkarych/kora/issues/140) (pending): retry, DSL, code host, target, skills, budgets, notifications, mounted MCP and the web console. Until #140 lands, those contracts are listed here as pending, and [ADR-0009](decisions/0009-target-repository-trust-boundary.md), [ADR-0024](decisions/0024-provider-limits-as-non-agent-failures.md) and items 1 and 5 of the roadmap still place the Codex confirmation, a trace per harness and the code-host contracts in the first slice.

The triggers are abbreviated as follows:

- **retry:** node retry policy and the retry and rerun operations;
- **DSL:** the compiler, `kora compile`, `kora test` and scenarios;
- **code host:** the `vcs` approval channel, change requests, a credential issuer and static credentials;
- **target:** the graph target and target holds;
- **Docker:** Docker and the protected runtimes;
- **starts:** cron, webhook, sensor and graph-triggered starts.

Other triggers are named directly. A "leg" is the part of a test that needs the trigger; the rest of the test runs in slice 1.

| Section | Work items | Pending in slice 1 |
|---|---|---|
| [protocols.md section 3](../knowledge/specs/protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) | 14, 15 | `test_skill_directory_digest_mismatch_prevents_session_start` (skills) |
| [protocols.md section 13](../knowledge/specs/protocols.md#13-conformance-suites-nfr-102) | runtime 6, 7, 14; harness 7, 15; VCS 7, 13, and 14 for the local-runtime legs of `test_vcs_runtime_cross_product`; approval channel 8, 16; secrets 14; artifact storage and artifact type 2, 8; authentication 8; policy 15; `test_workspace_preparation_recovers_after_worker_crash` 7, 13, 14, 17 | `test_publish_and_open_change_request_are_idempotent` (code host); the change-request leg of `test_publish_reconciles_after_response_loss` (code host); the Docker legs of `test_vcs_runtime_cross_product`; the local-Git fake VCS with its in-process code-host fake (code host); the Codex leg of `test_required_policy_is_enforced_or_rejected`; the sensor suite (starts, `wait_event`); the end-to-end canonical-graph suite (branch, fan-out, sub-graph, `wait_event`, timer, code host) |
| [External observation gates](../knowledge/specs/protocols.md#external-observation-conformance-gates) | 8, 13, 14, 16 | sensor references (starts); channel references (code host) |
| [Publication authority and recovery](../knowledge/specs/protocols.md#publication-authority-and-recovery-contracts) | 13, 14, 17; `test_non_git_inspection_stream_scans_intermediate_history` 7, 13; `test_harness_launcher_preserves_control_boundary` 14, 15; `test_required_execution_protection_rejects_local` is in the #140 list below | `test_candidate_cannot_forge_runner_journal` and `test_agent_descendants_cannot_access_publication_credentials` (Docker); `test_publication_authorization_is_late` (code host); the protected leg of `test_publication_sealing_rejects_mutable_aliases` (Docker); the Codex leg of the launcher test |
| [Command-resource contracts](../knowledge/specs/protocols.md#command-resource-contracts-issue-45) | 3, 8, 11, 14, 17 | `test_compile_exit_then_deploy` and the compiler leg of `test_resource_set_digest_vector` (DSL); `test_graph_runs_on_repository_without_gates` (branch, fan-out, sub-graph, `wait_event`, timer); `test_candidate_cannot_alter_resources` (Docker) |
| [Agent-secret contracts](../knowledge/specs/protocols.md#agent-secret-contracts-adr-0009-amended-2026-10-18) | 3, 9, 11, 12, 14 | `test_agent_cannot_read_mcp_server_secret` and `test_docker_egress_blocks_unlisted_host` (Docker); the rerun leg of `test_unenforced_secrets_require_explicit_option` (retry) |
| [Scenario contracts](../knowledge/specs/protocols.md#scenario-contracts-issue-46) | — | all (DSL) |
| [Code-host identity contracts](../knowledge/specs/protocols.md#code-host-identity-contracts-adr-0023) | — | all (code host) |
| [Provider-limit contracts](../knowledge/specs/protocols.md#provider-limit-contracts-adr-0024) | `test_internal_retry_is_liveness_not_stall` 7, 14, 15; the recorded `claude` trace 21 | the Codex trace; the other provider-limit contracts, in the #140 list below |
| [Notification contracts](../knowledge/specs/protocols.md#notification-contracts-adr-0025) | — | all (notifications) |
| [Execution-recovery gates](../knowledge/specs/protocols.md#execution-recovery-implementation-gates-adr-0017) | 9, 10, 12, 14, 17, 18; the partition test 21 | `test_clear_downstream_waits_for_execution_retirement` (retry); the retry legs of `test_reconnected_old_execution_cannot_publish_current_result` and `test_uncertain_execution_retains_reservations_after_cancel`; the runtime-level retry legs of `test_submit_crash_recovers_in_original_execution_domain` and `test_execution_replacement_requires_declared_authority`, in the #140 list below; the code-host legs of `test_recovery_retains_every_dispatcher_generation` and `test_runtime_stop_does_not_imply_publication_quiescence`; the target, cost-read and console legs of `test_uncertain_usage_and_recovery_surface_parity`; the Docker leg of `test_recovery_fences_survive_release` |
| [Immutable execution-history contracts](../knowledge/specs/protocols.md#immutable-execution-history-contracts) | as data-model.md section 11.4 | as data-model.md section 11.4 |
| [Claude adapter contracts](../knowledge/specs/protocols.md#claude-adapter-contracts-issue-125) | 3, 14, 15 | `test_claude_skills_delivered_as_plugin` (skills); the Docker leg of `test_claude_session_receives_only_constructed_environment`; the `budget_usd` leg of `test_claude_usage_from_stream_events`, listed below |
| [Inherited-deadline gates](../knowledge/specs/protocols.md#15-inherited-deadline-conformance-gates-adr-0022) | 10, 13, 14, 17 | nested-wrapper cases (sub-graph, fan-out); scope retry (retry); Docker evidence |
| [Durable scheduling marker](../knowledge/specs/data-model.md#21-durable-run-scheduling-marker) | 5, 10; the latency case also 21 | `test_settled_failed_retry_reenters_discovery` (retry) |
| [Current branch selection](../knowledge/specs/data-model.md#51-current-branch-selection) | — | all (branch) |
| [Clear downstream](../knowledge/specs/data-model.md#6-clear-downstream-fr-68a) | — | all (retry) |
| [Immutable contracts and execution records](../knowledge/specs/data-model.md#114-future-acceptance-contracts) | 9, 12, 13, 14, 17 | `test_failed_attempt_inputs_survive_clear_downstream`, `test_attempt_diff_uses_immutable_history` and `test_attempt_history_retention_and_retry` (retry); `test_running_graph_keeps_deployed_artifact_schema` (artifact-type routes, branch, fan-out) |
| [Immutable run target identity](../knowledge/specs/data-model.md#125-future-acceptance-contracts) | — | all (target) |
| [Run acceptance and execution capacity](../knowledge/specs/data-model.md#134-future-acceptance-contracts) | 9, 10, 11, 12; `test_run_acceptance_preserves_overlap_and_replay` in full | `test_deferred_target_does_not_block_independent_target` and `test_admission_skips_blocked_candidates` (target); the target, start and retry legs of `test_deferred_run_backlog_is_bounded`, `test_admission_capacity_released_once_across_waits`, `test_admission_preserves_unresolved_execution_reservations` and `test_admission_limits_are_atomic`; the trigger, policy-read and target legs of `test_admission_state_has_surface_parity` |
| [External observation and delivery ownership](../knowledge/specs/data-model.md#144-future-acceptance-contracts) | 9, 10, 13, 16, 18 | `test_start_sensor_result_compares_binding_atomically` (starts); `test_stale_observation_cannot_resolve_replaced_wait` (retry, target); the retry, push-event and sensor legs of `test_blocked_observation_does_not_hold_run_lock`, `test_duplicate_observation_is_idempotent` and `test_observation_apply_wakes_downstream_without_other_activity` |
| [Inherited execution deadlines](../knowledge/specs/data-model.md#153-recovery-and-future-acceptance-contracts) | 9, 10, 13, 14, 17, 21 | `test_subgraph_timeout_spans_retries_and_child_attempts` (sub-graph, retry); `test_retry_cannot_reset_enclosing_deadline` (retry); the wrapper, target-source and credential legs of `test_runner_enforces_earliest_enclosing_deadline`, `test_expired_deadline_revokes_result_authority` and `test_managed_phases_share_effective_deadline`; Docker evidence |
| [Heartbeats, stall detection and timeouts](../knowledge/specs/scheduler.md#4-heartbeats-stall-detection-and-timeouts) | `test_runtime_enforces_deadline_during_control_plane_partition` 10, 14, 17, 21 | `test_parallel_attempts_share_run_budget`, `test_subgraph_budget_includes_previous_iterations` and `test_resumed_session_does_not_double_count_usage` (budgets, sub-graph, retry) |
| [Waits hold no worker](../knowledge/specs/scheduler.md#5-waits-hold-no-worker-nfr-13) | 16, 18 for the head-change cases | `test_push_cannot_rebind_old_evidence` (retry, push webhooks) |
| [Concurrency limits](../knowledge/specs/scheduler.md#6-concurrency-limits-fr-610) | `test_last_slot_reserved_once`, `test_created_attempt_reserves_capacity`, `test_capacity_released_once` 5, 9, 10 | `test_same_target_triggers_follow_declared_policy`, `test_target_hold_taken_once`, `test_target_bound_from_artifact_version`, `test_failed_run_releases_target` and `test_target_projection_names_accepted_change` (target) |
| [Retry handling](../knowledge/specs/scheduler.md#7-retry-handling) | — | all (retry; also Docker) |
| [Scheduling latency](../knowledge/specs/scheduler.md#9-scheduling-latency-nfr-31) | 10, 21 | the fan-out variant of NFR-3.3 |
| [Diagnostic output health](../knowledge/specs/scheduler.md#diagnostic-output-health) | 1, 10, 17, 20 | — |
| [External observation dispatch](../knowledge/specs/scheduler.md#16-external-observation-dispatch-adr-0021) and [inherited-deadline enforcement](../knowledge/specs/scheduler.md#17-inherited-deadline-enforcement-adr-0022) | 16; 10, 14 | as their data-model sections; sensor purposes (starts) |
| [Scheduler-prototype milestone](../knowledge/specs/scheduler.md#18-scheduler-prototype-milestone-acceptance) and [notification delivery](../knowledge/specs/scheduler.md#19-notification-delivery-adr-0025) | — | all (the milestone; notifications) |
| [API conventions and structured errors](../knowledge/specs/api-and-agent-surface.md#structured-errors-fr-04) | 11 | — |
| [Bounded run-read wait](../knowledge/specs/api-and-agent-surface.md#bounded-run-read-wait-adr-0002) | 19, with 11, 12 and 18; `test_client_waiting_read_is_bounded` and `test_cli_run_wait_is_bounded` in full | the retry, fan-out and mounted-MCP legs of `test_run_read_wait_is_bounded` and of its measurements |
| [MCP servers](../knowledge/specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28) | 12, 19 | `test_mcp_mounted_calls_carry_their_own_credential` (mounted MCP); `test_mcp_kora_test_cancellation_releases_processes` (DSL); the mounted and retry legs of `test_mcp_trigger_returns_without_waiting`, `test_mcp_skip_and_refusal_return_at_once` and `test_mcp_runs_wait_is_bounded` |
| [Parity checklist](../knowledge/specs/api-and-agent-surface.md#7-parity-checklist-fr-03-release-gate) | 11, 18 | `test_parity_client_side_operations`, in the #140 list below; the generated TypeScript client (web console); the skill items (skills, `/reference/*`) |
| [CEL conformance](../knowledge/specs/ir.md#78-conformance) and [CEL future contracts](../knowledge/specs/ir.md#79-future-contracts) | 4, with 3, 5, 9, 11, 12, 18 and 20 | listed below |
| [Worked examples](../knowledge/specs/ir.md#11-worked-examples), [DSL compile step](../knowledge/specs/dsl.md#2-compile-step-kora-compile) and [testing graphs without model calls](../knowledge/specs/dsl.md#4-testing-graphs-without-model-calls-nfr-101) | — | all (the canonical-graph fixture; DSL) |
| [Future executable evidence](../knowledge/specs/observability.md#8-future-executable-evidence) | `test_logging_schema_levels_and_component_overrides` 1; `test_logging_context_isolated_and_propagated` 1, 10, 11, 14, 16, 17; `test_api_request_id_matches_response_error_and_logs` and `test_cli_and_mcp_stdout_remains_machine_readable` 11; `test_logging_redacts_content_and_safe_exception_chains` 1, 3, 5, 11, 14; `test_candidate_output_cannot_forge_component_diagnostics` 14, 15; `test_logging_redaction_state_is_bounded_across_rotation` 14; `test_logging_multiprocess_framing_with_slow_reader` 14, 17; `test_logging_sink_failure_does_not_stall_work` 1, 10, 16, 17; `test_logging_volume_tracks_transitions_not_poll_frequency` 10, 16, 17, 19; `test_log_filtering_does_not_change_durable_events` 5, 9, 10, 11; provider logging conformance 6, 7, 8, 13, 14, 15 | the browser scenarios (web console); the protected leg of the forged-diagnostics test (Docker) |
| Contracts that pending design issues name | [#135](https://github.com/realkarych/kora/issues/135) 3, 8, 11; [#136](https://github.com/realkarych/kora/issues/136) 7, 17, 20; [#137](https://github.com/realkarych/kora/issues/137) 2, 3; [#138](https://github.com/realkarych/kora/issues/138) 2, 8; [#139](https://github.com/realkarych/kora/issues/139) 9, 17; [#140](https://github.com/realkarych/kora/issues/140) 3, 11, 13; [#141](https://github.com/realkarych/kora/issues/141) 8, 11, 14, 18, 20; [#142](https://github.com/realkarych/kora/issues/142) 9, 15 | as each issue settles |

**Pending CEL contracts.** Of the [CEL future contracts](../knowledge/specs/ir.md#79-future-contracts), these wait for a trigger:

- **DSL:**
  - `test_kora_compile_and_validate_report_same_location`;
  - `test_kora_compile_does_not_import_cel_binding`;
  - the compile leg of `test_cel_regex_compile_cost_bounded`;
  - the `kora test` leg of `test_cel_evaluating_entry_points_refuse_without_binding`.
- **`/plan`:** that leg of `test_deploy_rejects_hand_written_ir_over_cel_limit`.
- **Sub-graphs and fan-out:**
  - `test_cel_projection_composes_through_collect_and_exports`;
  - every leg of `test_cel_scheduler_produced_outputs_are_projected` except the form response.
- **Target:** the target-source leg of `test_cel_oversized_projection_fails_producer_with_artifact_invalid`.
- **Starts, rerun and code host:** the webhook, rerun, trigger-parameter and polled-response legs of `test_cel_rejects_over_bound_data_at_entry`.
- **`wait_event` and sensors:**
  - `test_cel_external_projection_over_bound_is_no_match`;
  - `test_cel_match_evaluated_once_per_wait`;
  - `test_cel_until_error_keeps_waiting_and_is_visible`;
  - `test_cel_until_error_does_not_degrade_integration`.
- **Retry:**
  - `test_cel_retry_predicate_ignores_invalid_outputs`;
  - `test_cel_retry_predicate_error_keeps_original_class`;
  - the manual-retry leg of `test_cel_projection_absent_optional_is_absent`.
- **Branch:**
  - `test_cel_branch_error_fails_with_config_error`;
  - `test_cel_evaluation_allowance_yields_between_batches`, which needs an expression kind under the scheduler's evaluation allowance: a branch, a retry predicate, a wake-up or a polled observation.
- **Code host:** the channel-path legs of `test_cel_approve_when_error_marks_request_failed` and `test_cel_approve_when_not_evaluated_without_authority`.
- **Starts and the web console:** the trigger-binding and TypeScript legs of `test_cel_evaluation_error_reads_parity` and `test_cel_persisted_errors_exclude_values`.

**Contracts whose slice-1 status [#140](https://github.com/realkarych/kora/issues/140) settles (pending).** Neither the slice scope nor its exclusions decide these yet:

- the `s3` leg of the artifact-storage conformance suite;
- the observability-exporter suite and its provider row;
- the runtime-level retries of `provider_limited` and `infrastructure`, which never evaluate the node retry policy ([scheduler.md section 7](../knowledge/specs/scheduler.md#7-retry-handling)):
  - `test_provider_limit_does_not_consume_agent_attempts`;
  - `test_provider_limit_ceiling_fails_node`;
  - `test_provider_limit_keeps_partial_work`;
  - `test_provider_wait_counts_towards_inherited_deadline`;
  - the runtime-level retry legs of `test_submit_crash_recovers_in_original_execution_domain` and `test_execution_replacement_requires_declared_authority`;
- `test_parity_client_side_operations`, whose compile and test pairs are outside slice 1 if the DSL is;
- the admission pause of ADR-0024:
  - `test_provider_limit_pauses_admission_for_quota`;
  - `test_provider_release_is_paced`;
  - `test_provider_wait_budget_covers_never_attempted_peer`;
- the per-attempt `budget_usd`;
- cases that need a seeded or changed namespace policy:
  - `test_required_execution_protection_rejects_local`;
  - `test_unprotected_runtime_reports_protection_none`;
  - `test_agent_secret_rule_fails_closed`;
  - `test_historical_attempt_keeps_effective_policy`;
  - the policy legs of `test_admission_limits_are_atomic`, `test_approval_requires_fresh_current_head_observation` and `test_admission_state_has_surface_parity`;
  - the namespace-pattern legs of `test_required_policy_is_enforced_or_rejected`;
- the retention task's legs of `test_attempt_history_retention_and_retry`, `test_attempt_diff_uses_immutable_history` and `test_cel_projection_absent_optional_is_absent`;
- the `kora doctor` leg of `test_cel_evaluating_entry_points_refuse_without_binding`;
- the graph-version retirement leg of `test_admission_limits_are_atomic`;
- `test_cel_examples_pass_checker`, which depends on the node kinds work item 2 models.

## 8. Observability, threat-model evidence and measurements

**Observability activation rows** ([observability.md section 7](../knowledge/specs/observability.md#7-component-coverage-and-activation)). A row activates with the first implementation of its component:

| Row | Work item |
|---|---|
| Logging infrastructure, the shared bootstrap and the common tests | 1 |
| DSL/compiler/IR | 2, 3; the compiler part is pending: DSL |
| Metadata/migrations/retention | 5 |
| Runtime, harness adapter and VCS provider rows | 7 for the fakes; 13, 14 and 15 add the evidence of the real implementations |
| Authentication, artifact storage, artifact type and approval channel provider rows | 8 |
| Scheduler/admission/retries | 10 |
| API and webhooks; shared clients/CLI/MCP | 11 |
| Runtime supervisor/common executor | 13 for the common executor, 14 for the runner |
| Secrets provider row | 14 |
| Session runner/harness bridge | 14, 15 |
| Policy provider row | 15 |
| Sensors and observation dispatcher | 16 |
| Worker | 17 |
| Startup/doctor/deployment | 20 |
| Browser; product harness instructions; sensor and observability-exporter provider rows | pending: the web console, skills, sensors and the exporter |

**Threat-model evidence** ([threat model](threat-model.md#verification-and-residual-limitations)):

| Threats | Evidence and work item |
|---|---|
| TM-01, TM-02 | Denied access and service and user separation: 11 (route-authorization coverage), 12, 17, 18 and 20 (tokens). Authentication and approval-channel conformance: 8 and 16. Authorization during the run-read wait: 19. Session, CSRF, webhook and mounted-MCP cases are pending: the web console, webhooks and mounted MCP |
| TM-03, TM-04 | `test_required_policy_is_enforced_or_rejected`, `test_harness_launcher_preserves_control_boundary` and the `claude` scripted-model cases: 15. Closed type contracts (`IR022`): 2 and 3. Expression bounds and the CEL function restriction: 4. The template sandbox: 17 |
| TM-05, TM-11 | `test_candidate_git_configuration_cannot_execute_in_publisher`, `test_publication_destination_is_trusted`, `test_publication_preserves_candidate_commits` and the local legs of `test_publication_sealing_rejects_mutable_aliases`: 13, 14 and 17; stored-reference cases: 8. Pending: `test_candidate_cannot_forge_runner_journal` and `test_agent_descendants_cannot_access_publication_credentials` (Docker), late authorization (code host), target-source upload replay (target) |
| TM-06, TM-07 | `test_claude_session_receives_only_constructed_environment` on local: 15. `IR007`: 3. Secret-prefix rejection and redaction: 14. Retained-projection authorization and payload expiry: 12. Pending: the Docker leg, sensor destinations, runtime network-capability conformance and the browser checks |
| TM-08, TM-09 | Stale approvals, changed digests and the CEL approval cases on the API path: 9 and 18. Unknown execution and dispatcher closure: 9, 14 and 17. Pending: replaced waits and late results that need retry or wait events, and the channel-path legs (code host) |
| TM-10 | Admission races on both databases: 9. Deadline enforcement: 10, 13, 14 and 17. The bounded run-read wait: 19 |
| TM-12 | `test_claude_binary_digest_mismatch_prevents_session_start`: 15. Installed-provider discovery: 6. The CEL binding's packaging cases: 4. A serving process that never selects a fake: 17 and 20, per #136 (pending) |

**Measurements:**

| Measurement | Work item |
|---|---|
| The run-read wait measurements of [api-and-agent-surface.md](../knowledge/specs/api-and-agent-surface.md#bounded-run-read-wait-adr-0002); their process-exit leg runs once work item 20 exists | 19; the retry, fan-out and mounted-MCP legs are pending |
| `test_claude_stream_volume_is_bounded` and `test_claude_session_releases_resources` | 15 |
| CEL validation time, crash thresholds and lifetime ([ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates)) | 4 |
| CEL under load: approval and start latency while validation runs | 18 |
| CEL in standalone: event-loop lag and start and stop cycles | 20 |
| CEL under the scheduler's evaluation allowance, many waits, fan-out and `kora test` cycles | pending: branch, retry, `wait_event`, fan-out, DSL |
| NFR-3.1 scheduling latency: the scheduler part | 10 |
| NFR-3.1 scheduling latency: end to end | 21 |
| Logging overhead and bounded output ([observability.md section 8](../knowledge/specs/observability.md#8-future-executable-evidence)) | 1 |
