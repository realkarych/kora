# Implementation plan

VcsRef revision-1 amendment dated 2026-10-23: the project owner approved the contract plan and the exact wording in this document under [issue #208](https://github.com/realkarych/kora/issues/208#issuecomment-6353268608).

Scope and implementation plan approved by the project owner under [issue #127](https://github.com/realkarych/kora/issues/127) on 2026-10-19; wording pending line-by-line owner review.

Shared diagnostic amendment dated 2026-10-23: the #207 plan and exact changes below were reviewed and approved by the project owner; the numeric bound was separately approved on the same date.

Approval authoring amendment dated 2026-10-23: the default form, conditional approval predicate and fixed implicit output names are specified under [issue #137](https://github.com/realkarych/kora/issues/137). Their executable model, validator, compiler and approval-transition checks remain pending in the work items below.

IR first models amendment dated 2026-10-22: work item 2's first part lands under [issue #201](https://github.com/realkarych/kora/issues/201); wording pending line-by-line owner review.

Protocol foundation amendment dated 2026-10-22: work item 6's second part splits into three parts and its foundation lands under [issue #204](https://github.com/realkarych/kora/issues/204), as the [#156 parts plan](https://github.com/realkarych/kora/issues/156#issuecomment-6347952676) states; wording pending line-by-line owner review.

CEL evaluator amendment dated 2026-10-21: infrastructure and verification added under [issue #166](https://github.com/realkarych/kora/issues/166); wording accepted by the project owner on 2026-10-22.

CEL cost-regression amendment dated 2026-10-23: the project owner approved lightweight validation and a 100 ms target under [issue #167](https://github.com/realkarych/kora/issues/167#issuecomment-6351849696); wording reviewed line by line and approved by the project owner on 2026-10-23.

Local-runtime simplification amendment dated 2026-10-22: under [issue #194](https://github.com/realkarych/kora/issues/194), the measurement rows of issue #123 drop the delegated scope and the disk reserves, and add the peer-attestation cost, the full-disk fallback, the retry and the index-disabled resume measurements; wording pending line-by-line owner review.

Step 8 amendment dated 2026-10-21: the fake harness and fake runtime contract of [issue #136](https://github.com/realkarych/kora/issues/136) is merged; wording pending line-by-line owner review.

Step 6 amendment dated 2026-10-21: the worker route models and the terminal report of [issue #139](https://github.com/realkarych/kora/issues/139) are merged; wording pending line-by-line owner review.

Step 5a local-runner amendment dated 2026-10-21: the local runtime, the transfer channel and the harness launcher of [issue #123](https://github.com/realkarych/kora/issues/123) are merged; wording pending line-by-line owner review.

Step 5a amendment dated 2026-10-21: the field lists of [issue #134](https://github.com/realkarych/kora/issues/134) are merged; wording pending line-by-line owner review.

Work item 1 amendment dated 2026-10-20: landed under [issue #147](https://github.com/realkarych/kora/issues/147); wording pending line-by-line owner review.

Work item 4 amendment dated 2026-10-20: split by the owner under [issue #160](https://github.com/realkarych/kora/issues/160); its first part, `kora.cel`, landed under [issue #161](https://github.com/realkarych/kora/issues/161); wording pending line-by-line owner review.

Work item 6 amendment dated 2026-10-21: split into two issues; its first part, provider loading, the JSON and error types, `RuntimeCapabilities`, `ExecutionDeadline`, the local deadline thresholds, `Clock` and `ActivityReporter`, landed under [issue #150](https://github.com/realkarych/kora/issues/150), and the rest is [issue #156](https://github.com/realkarych/kora/issues/156); wording pending line-by-line owner review.

Work item 5 amendment dated 2026-10-20: landed under [issue #153](https://github.com/realkarych/kora/issues/153); the rows of freeze steps 2 and 3, the lock-order gap and the timeout-discovery question record [#102](https://github.com/realkarych/kora/issues/102) and [#103](https://github.com/realkarych/kora/issues/103) as landed; wording pending line-by-line owner review.

This page orders the work of the first implementation slice so that two developers can work in parallel with little overlap. It records no decisions of its own. The decisions are in [decisions/](decisions/README.md), and the contracts are in the [specifications](../knowledge/README.md).

Where a contract is still open, the page names the issue that settles it, marked *pending*. Until that issue lands, the merged text governs and the work that depends on it waits. The page does not restate what a pending issue proposes.

The page's own content is the order of work: when each shared contract freezes, which side owns it, and the work items in order. The page is updated in three cases:

- a freeze step merges;
- an issue it cites lands. Each design issue opened for this plan (#134 to #142) carries the update of its row, its named contracts (section 7) and its threat-model evidence (section 8) as an acceptance criterion. For the other cited issues, the pull request that lands them updates the page, because documents and their dependents change together;
- the owner changes the order.

Harness-scope amendment dated 2026-10-21: both Claude and Codex are required in the first slice; scope and implementation plan approved by the project owner under [issue #172](https://github.com/realkarych/kora/issues/172); wording pending line-by-line owner review.

## 1. Slice 1

**Scope.** The [ADR-0001 amendments of 2026-10-19 and 2026-10-21](decisions/0001-own-control-plane.md) define the slice:

- `kora standalone` on SQLite as the only deployment it ships;
- one agent node selectable between the `claude` adapter through the Claude Agent SDK and the `codex` adapter through Codex app-server, and one command node on the local runtime; both adapters are required, with the same minimal topology accepted independently for each;
- a manual trigger, a git `VcsRef` output and an approval that a person answers through the CLI;
- every integration test that touches the metadata database runs on PostgreSQL and on SQLite;
- a restarted worker adopts a live session;
- every API operation the slice adds meets the parity rule of [ADR-0002](decisions/0002-ai-first-api-and-thin-clients.md), with that record's exclusions and its client-side compilation, and the parity coverage test ships with the first operations;
- a long MCP operation the slice adds follows the MCP contract as changed under [issue #124](https://github.com/realkarych/kora/issues/124).

[DD-36](../knowledge/reviews/design-decisions-2026-10-15.md#dd-36-implementation-default-ownership) assigns the owners of its defaults.

[Issue #140](https://github.com/realkarych/kora/issues/140) (pending) settles the boundaries of the slice:

- what it refuses at deploy time, and how;
- which operations and MCP transports it ships;
- the remaining code-host and credential contracts it carries. Both harnesses and their required real policy-hook evidence are already part of the slice under #172.

Until #140 lands, the ADR-0001 amendment and the [roadmap](roadmap.md#what-the-documents-cannot-settle) govern.

**What it proves.** It proves items 1 to 3 of "What the documents cannot settle" in the roadmap, to the extent the slice contains them:

- the lifecycle behaviour of [ADR-0016](decisions/0016-trusted-execution-and-publication.md) and [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md) on the local runtime, in a trusted environment, with no claim of OS isolation;
- the Claude Agent SDK paths of the `claude` adapter and the Codex app-server paths of the `codex` adapter, each with its own real-interface evidence;
- the CEL binding, through `approve_when` ([ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates));
- the fake harness and fake runtime passing the same conformance suites as real implementations. Fakes prove core decisions, composition and recovery only, with no OS isolation or real harness interception claim ([threat model](threat-model.md#deployment-and-runtime-applicability)).

**How it is tested without model calls.**

- Integration tests run on the real API, scheduler and worker, with the fake harness and fake runtime (NFR-10.1 and the [testing rules](../AGENTS.md#testing)).
- The `claude` adapter is tested through the evidence layers of [protocols.md section 13](../knowledge/specs/protocols.md#claude-adapter-contracts-issue-125):
  - replay, which proves normalisation, not interception;
  - scripted model, the deterministic driver for interception, not yet observed to work with the tested CLI. Missing reproducible real policy-hook evidence blocks slice readiness under #172;
  - connect;
  - runtime, through the launcher on local, for the constructed environment, the binary digest and the release of resources;
  - manual, outside CI, which records the real-session, provider-limit and internal-retry traces.

  The connect and scripted-model layers run on the Linux job inside a container without an external network, and each case asserts that no outbound connection was attempted. A case the tested version cannot run that way moves to the manual layer, and the gap is reported; this does not waive the required reproducible real policy-hook matrix.
- The `codex` adapter must be tested with its own deterministic driver through the real app-server interface for the policy side-effect matrix, managed-hook loading, hermetic configuration, launcher argv/env/cwd, interruption, nested tool/helper processes, event and usage normalisation, installed-version checks and resource release. Its implementing issue in work item 15 defines the driver and named cases before code; it does not assume that the Claude scripted-model driver works for Codex. No-model CI cases exclude external network access and assert that no outbound connection was attempted. Replay proves normalisation only. Manual experiments outside CI record real-session, provider-limit and internal-retry traces with the tested version; a manual smoke trace does not replace the reproducible real policy-hook matrix. If either harness cannot supply that matrix, slice readiness is blocked.
- Git runs for real against local repositories.
- `tick` runs with an injected clock ([issue #103](https://github.com/realkarych/kora/issues/103), landed in [PR #146](https://github.com/realkarych/kora/pull/146)).
- PostgreSQL runs through containers on the Linux job, and SQLite runs everywhere.
- The IR example job checks the slice-1 topology with each harness selection. Work item 21 runs the same topology end to end independently with each real adapter; fake runs alone cannot establish either adapter's readiness.

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
| 2 | Shared transition functions, `tick` and the lock order, including how a claim takes the run lock | [#103](https://github.com/realkarych/kora/issues/103), landed in [PR #146](https://github.com/realkarych/kora/pull/146) | server | work item 9 |
| 3 | Run outcome | [#102](https://github.com/realkarych/kora/issues/102), landed in [PR #145](https://github.com/realkarych/kora/pull/145) | server | work item 9 |
| 4 | Built-in artifact types, revision 1, and the names of implicit outputs | `VcsRef` is published first under [#208](https://github.com/realkarych/kora/issues/208); [#138](https://github.com/realkarych/kora/issues/138) remains open for `CommandResult`, `AgentSession` and `StructuredOutput`; [#137](https://github.com/realkarych/kora/issues/137), specified in [ir.md sections 3–4.2](../knowledge/specs/ir.md#3-nodes) | server | work item 2; a published specification does not imply its models or consumers are implemented |
| 5a | Runtime, harness, VCS, policy and secrets models, the transfer channel, the harness launcher and its directories, command inputs | [#106](https://github.com/realkarych/kora/issues/106) (pending); the [#123](https://github.com/realkarych/kora/issues/123) local runtime and harness launcher are merged in [protocols.md section 2](../knowledge/specs/protocols.md#local-runtime-issue-123) and [section 3](../knowledge/specs/protocols.md#harness-launcher-issue-123); the [#134](https://github.com/realkarych/kora/issues/134) field lists are merged in [protocols.md](../knowledge/specs/protocols.md) sections 2, 3, 4, 6 and 12; the `claude` adapter text is merged in [protocols.md section 3](../knowledge/specs/protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) | execution | work item 6 |
| 5b | Authentication, artifact storage, artifact type and approval-channel protocols | merged in [protocols.md](../knowledge/specs/protocols.md) sections 5, 7, 8 and 10; section 10 changes under [#135](https://github.com/realkarych/kora/issues/135) and section 8 under [#138](https://github.com/realkarych/kora/issues/138) (pending) | server | work item 8 |
| 6 | Worker route models and the attempt's terminal report | [#139](https://github.com/realkarych/kora/issues/139), merged in [api-and-agent-surface.md](../knowledge/specs/api-and-agent-surface.md#worker-route-bodies-issue-139), data-model.md, scheduler.md and protocols.md section 2 | execution | the terminal-report types in work item 6 once this step lands, no later than work item 9; the route bodies in work item 17 |
| 7 | Error model and route authorization | Shared diagnostic representation: [#207](https://github.com/realkarych/kora/issues/207); remaining envelope, status and authorization contracts: [#135](https://github.com/realkarych/kora/issues/135) (pending) | server | a separately approved diagnostic foundation in `ir` before the work-item-3 validator and the policy/harness part of work item 6; other error models in work item 3; route authorization in work item 11 |
| 8 | Fake harness and fake runtime contract, including what a serving process may select | [#136](https://github.com/realkarych/kora/issues/136), merged in [fakes.md](../knowledge/specs/fakes.md) | execution | work item 7 |

Until a step lands, the work items that depend on it wait. Step 7 merges before any route is implemented, including the worker routes of step 6, and before the validator's diagnostics.

## 3. Gaps found before implementation

On 2026-10-19 a check of the specifications against slice 1 found the gaps below. Until each issue lands, the cited text governs and the dependent work items wait.

| Gap | Settled in |
|---|---|
| A successful attempt cannot record runtime quiescence and dispatcher closure before its terminal status, as [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md) requires. A failed command gate cannot reach `command_failed`, and a failure before submit has no transition | [#139](https://github.com/realkarych/kora/issues/139), merged; [#123](https://github.com/realkarych/kora/issues/123), merged: the runner's normal-completion record and `quiescence(handle)` in [protocols.md section 2](../knowledge/specs/protocols.md#local-runtime-issue-123) |
| A claim through `SELECT … FOR UPDATE SKIP LOCKED` locks the attempt row before the run lock, against the lock order of [scheduler.md section 6](../knowledge/specs/scheduler.md#6-concurrency-limits-fr-610) | [#103](https://github.com/realkarych/kora/issues/103), landed in [PR #146](https://github.com/realkarych/kora/pull/146) |
| The built-in types `VcsRef`, `CommandResult`, `AgentSession` and `StructuredOutput` have no published schemas or digest fields | `VcsRef` under [#208](https://github.com/realkarych/kora/issues/208); [#138](https://github.com/realkarych/kora/issues/138) remains pending for `CommandResult`, `AgentSession` and `StructuredOutput` |
| `approve_when` is optional and has no default | [#137](https://github.com/realkarych/kora/issues/137), specified in [ir.md section 4.2](../knowledge/specs/ir.md#42-human-input-node-config-fr-22-fr-7); executable validation and approval processing remain in work items 2–4, 9 and 18 |
| There is no rule for naming outputs | [#137](https://github.com/realkarych/kora/issues/137), specified in [ir.md section 3](../knowledge/specs/ir.md#3-nodes); executable models and validation remain in work items 2–3, and authoring insertion lands with the DSL compiler |
| There is no unauthenticated error code and no code-to-status table, and the response envelope carries one error | [#135](https://github.com/realkarych/kora/issues/135) (pending) |
| The committed JSON Schema of the graph is hand-written | Decided by [ADR-0013](decisions/0013-implementation-stack.md): the Pydantic models are the single definition and publish `ir.schema.json`. Work item 2 replaces the hand-written draft, and the IR example job checks the generated file |
| The MCP server's default token lacks `approve`, but standalone issues a single token with both flags | [#141](https://github.com/realkarych/kora/issues/141) (pending) |
| The fake harness and fake runtime have no written contract | [#136](https://github.com/realkarych/kora/issues/136), merged in [fakes.md](../knowledge/specs/fakes.md) |

## 4. Further open questions that slice 1 touches

Rows that name a work item are settled in that work item's issue before its code, as the [open implementation questions](../knowledge/architecture/detailed.md#open-implementation-questions) expect.

| Question | Settled in |
|---|---|
| Price ownership, needed for the run cost on approval requests and the runner's usage relay; with it, token semantics across harnesses | [#142](https://github.com/realkarych/kora/issues/142) (pending) |
| Timeout discovery and locking | [#103](https://github.com/realkarych/kora/issues/103), landed in [PR #146](https://github.com/realkarych/kora/pull/146) |
| Shape of client transport failures | [#135](https://github.com/realkarych/kora/issues/135) (pending) |
| Exposure of the unauthenticated health and metrics routes | [#135](https://github.com/realkarych/kora/issues/135) and [#140](https://github.com/realkarych/kora/issues/140) (pending) |
| Evidence that a candidate process group is still the original one before fallback retirement ([owner comment](https://github.com/realkarych/kora/issues/123#issuecomment-6270810525)) | [#123](https://github.com/realkarych/kora/issues/123), merged in [protocols.md section 2](../knowledge/specs/protocols.md#local-runtime-issue-123) ("Honest receipts"), then work item 14 |
| Adoption on Linux when a service manager runs the worker | [#123](https://github.com/realkarych/kora/issues/123), merged in [protocols.md section 2](../knowledge/specs/protocols.md#local-runtime-issue-123) ("Linux service managers"), then work item 14 on Linux CI |
| Health serving (open question "Health serving") | work item 20, through the API process ([api-and-agent-surface.md section 2](../knowledge/specs/api-and-agent-surface.md#2-resources-and-operations), [scheduler.md section 11](../knowledge/specs/scheduler.md#11-metrics-and-health-nfr-73)) |
| Secret-prefix ownership (open question "Secret prefix ownership") | until it is settled, the provider configuration of [protocols.md section 6](../knowledge/specs/protocols.md#6-secrets-fr-101) governs: work item 3 applies `IR007` to the prefixes it declares, and work item 14 resolves against the same source, with the exclusion of [#141](https://github.com/realkarych/kora/issues/141) (pending) |
| Attribution of scheduling-latency intervals | work item 10 |
| Authorization of follow streams | work item 12 |
| Whether the MCP SDK brings its own HTTP stack | work item 11 |
| Bounds for prompt and command templates, rendered when the request is sealed ([ir.md section 7.10](../knowledge/specs/ir.md#710-prompt-templates)) | work item 17; [#139](https://github.com/realkarych/kora/issues/139) (pending) owns the sealing route |

Dry-run placement, skill delivery and the error class of a digest mismatch stay open questions; whether slice 1 needs them is settled in #140 (pending). The remaining open implementation questions do not block slice 1.

## 5. Two sides

| Side | Packages and providers | Shared contracts it owns |
|---|---|---|
| Server | `ir`, the CEL parser, checker and evaluator in the packages #104 assigns, `db`, `engine`, `scheduler`, the public routes of `api` and their `models`, `client`, `cli`, `mcp`, the logging bootstrap; the static-token authenticator, local artifact storage, the CLI approval channel, the artifact-type provider, the observation dispatcher, `kora standalone` | steps 1, 2, 3, 4, 5b, 7 |
| Execution | `protocols` with provider loading; the fake harness and fake runtime; `runner`; the local runtime with the env secrets provider; `executor` with git; the `allow_deny` policy; the `claude` and `codex` adapters; `worker`, the worker routes, their `models` and their client methods | steps 5a, 6, 8 |

The worker routes are handlers in the API application that call the shared transition functions. The execution side writes them, and the server side reviews every change to a transition function. People take a side as issue assignees.

Every API operation ships in one pull request with its CLI command, its MCP tool and its entry in the parity coverage test ([ADR-0002](decisions/0002-ai-first-api-and-thin-clients.md), [AGENTS.md](../AGENTS.md#code)). Each work item's issue lists:

- the named tests it carries (section 7);
- the threat-model evidence, observability activation rows and measurements assigned to it (section 8).

## 6. Work items

The work items are not issues yet. Each is opened when the owner asks, with the change form, the assignee of its side and checkable acceptance criteria.

| # | Work item | Side | Depends on |
|---|---|---|---|
| 1 | Logging bootstrap and process configuration, with the common logging tests run through the first process entry point it adds; the static-analysis, dependency and secret scanners in CI, placed here by the owner on 2026-10-19. Landed under [issue #147](https://github.com/realkarych/kora/issues/147) | server | step 1 |
| 2 | `ir` models of the whole IR and the built-in artifact types of revision 1; the generated `ir.schema.json` replaces the hand-written draft, with a check that the committed schema matches the models. Its first part, `ErrorClass`, `ToolPolicy` and the bounded JSON types moved down from `protocols`, landed under [issue #201](https://github.com/realkarych/kora/issues/201) for the policy and harness part of work item 6; `AgentConfig`, the node limits and the remaining graph/node models stay with this work item. The artifact-model split is described below; the validator stays with work item 3; the shared diagnostics are a separately approved foundation under `ir`, after the representation decision in #207 and before their consumers | server | steps 1, 4 |
| 3 | `ir` validator: collected diagnostics using the shared foundation specified by #207, the refusal of features outside the slice per #140, a slice-1 graph example. It needs the provider models of work item 6, so this server item waits for an execution item | server | 2, 4, 6; step 7; #140 |
| 4 | CEL parser, checker, projections and evaluation per [ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates), with the packaging, CI and development obligations of the "Expression language" row of the [stack table](../knowledge/architecture/detailed.md#stack-per-component). Split under [#160](https://github.com/realkarych/kora/issues/160): the parser, limits, rendering, paths, pattern subset and cost estimate in `kora.cel` landed under [#161](https://github.com/realkarych/kora/issues/161); the binding, evaluator, conformance through the binding, compact cost regression and lifetime measurements follow under [#162](https://github.com/realkarych/kora/issues/162); the schema-driven checker and projections wait for work item 2 | server | step 1; 2 for the schema-driven checker |
| 5 | `db`: tables, migrations, repositories and the two-database fixture; the claim and lock statements land with work item 9, and later work items add their own migrations. Landed under [issue #153](https://github.com/realkarych/kora/issues/153): the tables of data-model.md section 2, migration 0001, the identifier generator, the connection layer, the registry, discovery and occupancy read functions, `kora db upgrade`, the two-database fixture and the database lifetime measurement | server | 1; step 1 |
| 6 | `protocols` for runtime, harness, VCS, policy and secrets; provider loading; conformance-suite skeletons; the terminal-report types. Provider loading, the JSON and error types, the runtime capabilities and deadline types, the local deadline thresholds, `Clock` and `ActivityReporter` landed under [issue #150](https://github.com/realkarych/kora/issues/150); the rest is [issue #156](https://github.com/realkarych/kora/issues/156) in three parts: the foundation of `SecretValue`, `ProtocolContext`, process identity, the secrets protocol and the opt-in conformance plugin with its common and secrets cases ([issue #204](https://github.com/realkarych/kora/issues/204), which needs no `ir` model, proven by `test_secret_value_never_rendered`, `test_secret_admission_fails_closed`, `test_secret_scope_release_restores_registry`, `test_secret_transfer_field_is_masked`, `test_secret_reveal_is_confined`, `test_protocol_models_are_closed`, `test_json_object_field_is_bounded`, `test_conformance_requires_extra`, the conformance self-tests in `tests/conformance` and the `test_protocol_models_cost` measurement); the policy and harness part, after #201 and the shared diagnostic foundation specified by #207; and the runtime, evidence, report and VCS part, after the `VcsRef` contract (#208) and typed models (#209, work item 2) | execution | 1, 2; step 5a; step 6 for the terminal-report types |
| 7 | Fake harness, fake runtime and the directory-backed fake VCS | execution | 6, 13; step 8 |
| 8 | Server providers: static-token authentication, local artifact storage, the CLI approval channel, the artifact-type provider | server | 5, 6; steps 5b, 7; #141 |
| 9 | `engine`: canonical start, admission, approval and cancel transitions, worker-report transitions with the terminal report, completion and reconcile intent | server | 2, 4, 5, 7, 8; steps 2, 3, 6, 7; #142, #123 |
| 10 | `scheduler`: `tick`, the passes the slice needs, lease expiry, inherited-deadline timeouts and wake-up; the scheduler part of the NFR-3.1 measurement | server | 9 |
| 11 | API application and the graph operations; the parity coverage test, the published OpenAPI document, the client contract test and the route-authorization coverage test; the stdio MCP server | server | 3, 8, 9; step 7; #140, #141 |
| 12 | Run operations: trigger, run reads, events, nodes and attempts, logs, artifacts and cancel | server | 7, 11 |
| 13 | `executor` and the git provider, with head reads against a local repository | execution | 6; #140 |
| 14 | `runner` and the local runtime: registry, control channel, journal, transfer, retirement, adoption evidence and the harness launcher; the env secrets provider and redaction; the `allow_deny` policy | execution | 6, 7, 13; #123, #140, #141, #142 |
| 15 | `claude` and `codex` adapters, each with independent conformance, real policy-hook and launcher evidence, observability and resource-lifetime measurements. They may land in separate implementing issues and PRs, but both are required for slice acceptance | execution | 7, 14; #140, #142 |
| 16 | Observation dispatcher: CLI channel delivery and the head reads of VCS-bound approvals | server | 8, 10, 13 |
| 17 | `worker`, the worker routes and their client methods, request sealing with template rendering | execution | 7, 9, 11, 14; steps 6, 7, 8 |
| 18 | Approval operations and reconcile | server | 10, 12, 16; 17 for the end-to-end reconcile cases |
| 19 | Run-read wait ([api-and-agent-surface.md](../knowledge/specs/api-and-agent-surface.md#bounded-run-read-wait-adr-0002)); its process-exit cases run once work item 20 exists | server | 10, 12, 17, 18 |
| 20 | `kora standalone`: composition with both adapters registered and configurable, token issuance per #141 and health through the API process | server | 15, 17, 18, 19; step 8; #135, #140, #141 |
| 21 | Slice-1 acceptance: the same minimal graph through the CLI and the HTTP API independently with `claude` and `codex`, worker-restart adoption for each, `test_partitioned_live_host_is_not_assumed_stopped`, separate manual real-session, provider-limit and internal-retry traces for both adapters, and the end-to-end NFR-3.1 measurement | execution | 15, 20 |

The built-in artifact models are delivered separately from the remaining graph and node models. `VcsRef` and the shared `PrincipalRef` land under [#209](https://github.com/realkarych/kora/issues/209), following the immutable contract published under #208, so their complete typed boundary can unblock the runtime, evidence, report and VCS part of #156 without waiting for the other artifact models. `CommandResult`, `AgentSession` and `StructuredOutput` follow the remaining #138 contracts. The validator and shared diagnostics stay with work item 3 and #135; artifact storage and the artifact-type provider stay with work item 8.

The CEL work item is split under #160: parser/cost infrastructure (#161) and pinned binding/conformance (#165) are on `master`; evaluator infrastructure (#166) adds conversion, queues, cache, errors and lifetime evidence. The compact regression and golden corpus (#167) validate provisional settings against an observed 100 ms fixed-case target; they replace full fitting and crash-margin calibration as pre-consumer gates. Schema-driven checking/projections, correction of native minimum-int negation and real consumer integration remain required before those consumers execute author expressions. Their transaction, fairness and NFR-3.1 measurements remain mandatory; the logical budget and allowance do not bound elapsed time or RSS. The [scoped numerical exclusion](../knowledge/reviews/ai-development-setup.md#cel-numerical-calibration-exclusion) preserves existing lifetime and other component checks, including the `cel` diagnostics activation row and future API/scheduler error ownership.

## 7. Named contracts by work item

Every executable contract that the specifications name for a slice-1 component belongs to the work items below. A contract, or one leg of it, that needs a component outside the slice is pending until the trigger in the last column lands; the component gates stay with the slice that implements the component.

The VcsRef specification under #208 names `test_builtin_revision_1_schemas_are_immutable`, `test_builtin_digest_selection_is_stable`, `test_vcs_ref_rejects_invalid_payload`, `test_vcs_ref_schema_matches_model` and `test_vcs_ref_behavior_bounds` for the artifact-model part of work item 2. These remain pending until that implementation lands. JSON Schema checks prove structural acceptance only; the saved validation descriptor also defines strict decoding, aggregate byte bounds and canonical identity.

Runtime/VCS consumers in work items 6, 13 and 14 extend `test_vcs_repeated_finalize_returns_one_ref` to compare the complete saved push result across response loss and recovery. `test_vcs_ref_rejects_forged_publication_facts` covers trusted-operation matching in those consumers and the worker-result acceptance of work item 17. Code-host enrichment adds `test_vcs_ref_enrichment_replay_is_immutable` when work item 17 gains a code host; it does not delay push-only VcsRef consumers.

Upload consumers in work items 11, 12 and 17 extend `test_upload_requires_grant_before_bytes`, `test_refused_upload_bytes_are_reclaimed` and `test_target_source_replay_cannot_change_acceptance` for canonical VcsRef wire bytes, cumulative size and decoding refusal, full-payload/contract identity and owner-specific availability. Approval and observation consumers in work items 9, 10 and 17 cover supported revision bounds on both databases. None of these runtime gates is established by publishing the specification.

The ADR-0001 amendments place Docker, the trigger kinds other than manual, and the node kinds other than agent, command and human input outside slice 1. Both Claude and Codex, including their policy-hook and launcher evidence and separate traces, are required in slice 1 under #172. Every other trigger below follows the slice boundaries settled in [#140](https://github.com/realkarych/kora/issues/140) (pending): retry, DSL, code host, target, skills, budgets, notifications, mounted MCP, the web console, the artifact-type routes, the cost and policy reads, and `/plan`. Until #140 lands, those remaining contracts are listed here as pending; item 5 of the roadmap still places the code-host contracts in the first slice. The harness gates of [ADR-0009](decisions/0009-target-repository-trust-boundary.md), [ADR-0024](decisions/0024-provider-limits-as-non-agent-failures.md) and roadmap item 1 apply to both adapters now, independently of those pending decisions.

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
| [protocols.md section 13](../knowledge/specs/protocols.md#13-conformance-suites-nfr-102) | the plugin skeleton and the common cases 6 (issue #204); runtime 7, 14; harness 7, 15; VCS 7, 13, and 14 for the local-runtime legs of `test_vcs_runtime_cross_product`; approval channel 8, 16; secrets 6 against an injected provider (issue #204), 14; artifact storage and artifact type 8, with the built-in schemas of 2; authentication 8; policy 14, and 15 for both real harness hook legs; `test_workspace_preparation_recovers_after_worker_crash` 7, 13, 14, 17 | `test_publish_and_open_change_request_are_idempotent` (code host); the change-request leg of `test_publish_reconciles_after_response_loss` (code host); the Docker legs of `test_vcs_runtime_cross_product`; the local-Git fake VCS with its in-process code-host fake (code host); the sensor suite (starts, `wait_event`); the end-to-end canonical-graph suite (branch, fan-out, sub-graph, `wait_event`, timer, code host) |
| [External observation gates](../knowledge/specs/protocols.md#external-observation-conformance-gates) | 8, 13, 14, 16 | sensor references (starts); channel references (code host) |
| [Publication authority and recovery](../knowledge/specs/protocols.md#publication-authority-and-recovery-contracts) | 13, 14, 17; `test_non_git_inspection_stream_scans_intermediate_history` 7, 13; `test_harness_launcher_preserves_control_boundary` 14, 15; `test_required_execution_protection_rejects_local` is in the #140 list below | `test_candidate_cannot_forge_runner_journal` and `test_agent_descendants_cannot_access_publication_credentials` (Docker); `test_publication_authorization_is_late` (code host); the protected leg of `test_publication_sealing_rejects_mutable_aliases` (Docker) |
| [Protocol model contracts](../knowledge/specs/protocols.md#protocol-model-contracts-issue-134) | `test_secret_value_never_rendered` and `test_secret_reveal_is_confined` 6; `test_secret_registration_lifetime`, `test_adapter_cannot_emit_supervisor_kinds`, `test_tool_policy_layers` and `test_policy_failure_denies` 14; `test_handle_and_events_are_secret_and_capability_free`, `test_event_content_is_bounded_and_classified`, `test_runner_outcome_covers_every_stop` and `test_journal_backpressure_never_blocks` 14, 15; `test_runtime_observation_is_not_quiescence` 9, 17; `test_command_output_volume_is_bounded` 13, 14; `test_adapter_exception_order` 15; `test_finalise_memory_is_bounded` 7, 15; `test_session_spec_is_closed` 15, 17; `test_liveness_counters_reach_attempt` 17 | the seeded namespace-policy legs of `test_tool_policy_layers` (the #140 list) |
| [Worker route contracts](../knowledge/specs/api-and-agent-surface.md#worker-route-bodies-issue-139) | `test_successful_attempt_requires_closure_evidence`, `test_attempt_deadline_expiry_reaches_timeout`, `test_cancelled_attempt_reports_closure` and `test_lost_report_requires_retirement` 9, 10, 17; `test_success_pending_completion_is_rechecked` 9, 10; `test_adopted_attempt_reports_after_earlier_grants_close`, `test_result_report_requires_settled_publication`, `test_presubmit_refusal_reports_from_claimed` and `test_recovery_relay_closes_earlier_grants` 9, 17; `test_lost_claim_response_returns_the_same_grant` and `test_terminal_report_rejects_unbound_evidence` 9, 11; `test_failed_gate_reaches_command_failed` 9, 13, 17; `test_terminal_report_is_write_once`, `test_reserved_error_class_is_refused`, `test_unchanged_observation_keeps_recovery_version` and `test_usage_replacement_never_lowers_cost` 9; `test_segment_chunks_are_contiguous` 9, 12; `test_worker_write_requires_its_grant` 11, 17; `test_upload_requires_grant_before_bytes` 11, 12; `test_refused_upload_bytes_are_reclaimed` 5, 12 | none |
| [Local runtime contracts](../knowledge/specs/protocols.md#local-runtime-contracts-issue-123) | `test_worker_restart_adopts_live_session` 14, 17, 21; `test_dead_worker_dispatcher_closed_by_process_exit`, `test_live_old_worker_keeps_adoption_observational` and `test_worker_child_effect_keeps_dispatcher_open` 14, 17; `test_unconnected_grant_closed_by_process_exit` 9, 14, 17; `test_stale_incarnation_claim_binds_its_own_process` 9, 17; `test_launcher_takes_spec_from_private_channel` 14, 15; `test_runner_survives_worker_service_restart` 14 on Linux CI; `test_process_exit_evidence_requires_same_host_and_pid_namespace`, `test_lost_runner_is_reported_not_restarted`, `test_reused_pgid_is_never_signalled`, `test_receipt_names_its_scope`, `test_starting_entry_resolves`, `test_registry_states_and_release`, `test_completed_attempt_never_looks_lost`, `test_registry_lock_is_bounded`, `test_control_channel_rejects_impostors`, `test_journal_classes_and_caps`, `test_transfer_digest_mismatch_is_rejected`, `test_runner_environment_excludes_worker_credentials` and `test_state_root_and_socket_dir_refuse_unsafe_ownership` 14 | none |
| [Fake contracts](../knowledge/specs/fakes.md#10-contracts-and-measurements) | `test_serving_process_refuses_fake_providers` 6, 7, 17, 20; `test_fake_import_rule` active since #136; `test_production_runner_refuses_fake` 14; `test_testing_runner_hosts_fake_on_real_runtime`, `test_runner_enforced_classes_come_from_enforcement` and `test_fake_tool_calls_go_through_policy` 7, 14; `test_fake_harness_reads_only_its_script`, `test_fake_runtime_control_points`, `test_fake_runner_death_is_never_restarted`, `test_fake_runtime_releases_resources`, `test_unreleased_barrier_fails_within_watchdog` and `test_fakes_pass_their_conformance_rows` 7; `test_virtual_time_advances_only_when_quiescent` 7, 10, 17; `test_armed_action_fires_before_virtual_time_advances` 7, 20; `test_fake_results_carry_fake_evidence` 7, 17; `test_dry_run_resolves_no_secret` 7, 20, its dry-run leg pending #140 | none |
| [Command-resource contracts](../knowledge/specs/protocols.md#command-resource-contracts-issue-45) | 3, 8, 11, 14, 17 | `test_compile_exit_then_deploy` and the compiler leg of `test_resource_set_digest_vector` (DSL); `test_graph_runs_on_repository_without_gates` (branch, fan-out, sub-graph, `wait_event`, timer); `test_candidate_cannot_alter_resources` (Docker) |
| [Agent-secret contracts](../knowledge/specs/protocols.md#agent-secret-contracts-adr-0009-amended-2026-10-18) | 3, 9, 11, 12, 14 | `test_agent_cannot_read_mcp_server_secret` and `test_docker_egress_blocks_unlisted_host` (Docker); the rerun leg of `test_unenforced_secrets_require_explicit_option` (retry) |
| [Scenario contracts](../knowledge/specs/protocols.md#scenario-contracts-issue-46) | — | all (DSL) |
| [Code-host identity contracts](../knowledge/specs/protocols.md#code-host-identity-contracts-adr-0023) | — | all (code host) |
| [Provider-limit contracts](../knowledge/specs/protocols.md#provider-limit-contracts-adr-0024) | `test_internal_retry_is_liveness_not_stall` 7, 14, 15; the separate recorded `claude` and `codex` provider-limit and internal-retry traces 21 | the other provider-limit contracts, in the #140 list below |
| [Notification contracts](../knowledge/specs/protocols.md#notification-contracts-adr-0025) | — | all (notifications) |
| [Execution-recovery gates](../knowledge/specs/protocols.md#execution-recovery-implementation-gates-adr-0017) | 9, 10, 12, 13, 14, 17, 18; `test_partitioned_live_host_is_not_assumed_stopped` 21 | `test_clear_downstream_waits_for_execution_retirement` (retry); the retry legs of `test_reconnected_old_execution_cannot_publish_current_result` and `test_uncertain_execution_retains_reservations_after_cancel`; the runtime-level retry legs of `test_submit_crash_recovers_in_original_execution_domain` and `test_execution_replacement_requires_declared_authority`, in the #140 list below; the code-host legs of `test_recovery_retains_every_dispatcher_generation` and `test_runtime_stop_does_not_imply_publication_quiescence`; the target, cost-read and console legs of `test_uncertain_usage_and_recovery_surface_parity`; the Docker leg of `test_recovery_fences_survive_release` |
| [Immutable execution-history contracts](../knowledge/specs/protocols.md#immutable-execution-history-contracts) | as data-model.md section 11.4 | as data-model.md section 11.4 |
| [Claude adapter contracts](../knowledge/specs/protocols.md#claude-adapter-contracts-issue-125) | 3, 14, 15 | `test_claude_skills_delivered_as_plugin` (skills); the Docker leg of `test_claude_session_receives_only_constructed_environment`; the `budget_usd` leg of `test_claude_usage_from_stream_events`, listed below |
| Codex adapter evidence through the real app-server interface | 14, 15: `test_required_policy_is_enforced_or_rejected`, `test_harness_launcher_preserves_control_boundary`, `test_codex_session_receives_only_constructed_environment` and `test_codex_binary_digest_mismatch_prevents_session_start`; work item 15 defines its remaining named cases before code | Docker evidence and skills; the `budget_usd` leg follows #140 as for Claude; no local policy-hook, launcher or version-check leg is deferred |
| [Inherited-deadline gates](../knowledge/specs/protocols.md#15-inherited-deadline-conformance-gates-adr-0022) | 10, 13, 14, 17 | nested-wrapper cases (sub-graph, fan-out); scope retry (retry); Docker evidence |
| [Durable scheduling marker](../knowledge/specs/data-model.md#21-durable-run-scheduling-marker) | 5, 10; the latency case also 21 | `test_settled_failed_retry_reenters_discovery` (retry) |
| [Current branch selection](../knowledge/specs/data-model.md#51-current-branch-selection) | — | all (branch) |
| [Clear downstream](../knowledge/specs/data-model.md#6-clear-downstream-fr-68a) | — | all (retry) |
| [Immutable contracts and execution records](../knowledge/specs/data-model.md#114-future-acceptance-contracts) | 9, 12, 13, 14, 17 | `test_failed_attempt_inputs_survive_clear_downstream`, `test_attempt_diff_uses_immutable_history` and `test_attempt_history_retention_and_retry` (retry); `test_running_graph_keeps_deployed_artifact_schema` (artifact-type routes, branch, fan-out) |
| [Immutable run target identity](../knowledge/specs/data-model.md#125-future-acceptance-contracts) | — | all (target) |
| [Run acceptance and execution capacity](../knowledge/specs/data-model.md#134-future-acceptance-contracts) | 9, 10, 11, 12; `test_run_acceptance_preserves_overlap_and_replay` except its internal-rejection leg (starts) and its quota-change leg (the #140 list) | `test_deferred_target_does_not_block_independent_target` and `test_admission_skips_blocked_candidates` (target); the target, start and retry legs of `test_deferred_run_backlog_is_bounded`, `test_admission_capacity_released_once_across_waits`, `test_admission_preserves_unresolved_execution_reservations` and `test_admission_limits_are_atomic`; the trigger and target legs of `test_admission_state_has_surface_parity`; its policy-read legs are in the #140 list |
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
| [Parity checklist](../knowledge/specs/api-and-agent-surface.md#7-parity-checklist-fr-03-release-gate) | 11, 12, 18, 19 | `test_parity_client_side_operations`, in the #140 list below; the generated TypeScript client (web console); the skill items (skills, `/reference/*`) |
| [CEL conformance](../knowledge/specs/ir.md#78-conformance) and [CEL future contracts](../knowledge/specs/ir.md#79-future-contracts) | 4, with 3, 5, 9, 10, 11, 12, 18 and 20 | listed below |
| [Worked examples](../knowledge/specs/ir.md#11-worked-examples), [DSL compile step](../knowledge/specs/dsl.md#2-compile-step-kora-compile) and [testing graphs without model calls](../knowledge/specs/dsl.md#4-testing-graphs-without-model-calls-nfr-101) | — | all (the canonical-graph fixture; DSL) |
| [Future executable evidence](../knowledge/specs/observability.md#8-future-executable-evidence) | `test_logging_schema_levels_and_component_overrides` 1, 20; `test_logging_context_isolated_and_propagated` 1, 10, 11, 13, 14, 16, 17, 19; `test_api_request_id_matches_response_error_and_logs` 11, 17; `test_cli_and_mcp_stdout_remains_machine_readable` 11, 19; `test_logging_redacts_content_and_safe_exception_chains` 1, 3, 5, 11, 13, 14, 15, 16, 17, 20; `test_candidate_output_cannot_forge_component_diagnostics` 13, 14, 15; `test_logging_redaction_state_is_bounded_across_rotation` 14; `test_logging_multiprocess_framing_with_slow_reader` 13, 14, 17; `test_logging_sink_failure_does_not_stall_work` 1, 10, 13, 14, 16, 17, 20; `test_logging_volume_tracks_transitions_not_poll_frequency` 10, 16, 17, 19; `test_log_filtering_does_not_change_durable_events` 5, 9, 10, 11; provider logging conformance 6, 7, 8, 13, 14, 15 | the browser scenarios (web console); the protected leg of the forged-diagnostics test (Docker) |
| Shared diagnostic representation ([#207](https://github.com/realkarych/kora/issues/207)) | `test_shared_diagnostics_preserve_source_and_expression_offsets`, `test_shared_diagnostics_exclude_input_content` and `test_shared_diagnostic_codes_are_extensible`: the separate diagnostic foundation; `test_diagnostic_collections_are_bounded_and_count_omissions`: 3; `test_provider_diagnostics_rebase_without_losing_location`: 3 and 6; `test_kora_compile_and_validate_report_same_location`: 3 and 11, with the compiler leg pending its implementation | typed foundation first; consumer legs when each named component is implemented |
| Contracts that pending design issues name | [#102](https://github.com/realkarych/kora/issues/102) 9, 10; [#103](https://github.com/realkarych/kora/issues/103) 9, 10, 11; [#104](https://github.com/realkarych/kora/issues/104) 1; [#106](https://github.com/realkarych/kora/issues/106) 6, 14, 17; [#135](https://github.com/realkarych/kora/issues/135) 3, 8, 11, 20; [#138](https://github.com/realkarych/kora/issues/138) 2, 8; [#140](https://github.com/realkarych/kora/issues/140) 3, 11, 12, 13, 14, 15, 20; [#141](https://github.com/realkarych/kora/issues/141) 8, 11, 14, 18, 20; [#142](https://github.com/realkarych/kora/issues/142) 9, 14, 15 | as each issue settles |

The approval and output contracts of [issue #137](https://github.com/realkarych/kora/issues/137) remain pending implementation: `test_default_approval_form_and_expression` and `test_custom_form_without_decision_requires_approve_when` belong to work items 2–4 for model and expression validation and to 9 and 18 for approval processing. `test_implicit_output_names_are_fixed` belongs to 2–4 for declarations and effective response schemas, with runtime production in 9, 17 and 18 as each node kind activates. `test_reject_reports_authoritative_status` belongs to 18. Compiler insertion and scenario legs activate with the DSL compiler and `kora test`. Approval integration tests run on both databases; a passing schema example does not establish runtime behavior.

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
  - `test_historical_attempt_keeps_effective_policy`;
  - the policy legs of `test_unprotected_runtime_reports_protection_none` and `test_agent_secret_rule_fails_closed`, whose local legs run in work items 3, 14 and 17;
  - the policy legs of `test_admission_limits_are_atomic`, `test_approval_requires_fresh_current_head_observation` and `test_admission_state_has_surface_parity`;
  - the namespace-pattern legs of `test_required_policy_is_enforced_or_rejected`;
  - the quota-change leg of `test_run_acceptance_preserves_overlap_and_replay`;
- the retention task's leg of `test_cel_projection_absent_optional_is_absent`;
- the attempt-diff leg of `test_attempt_history_respects_transcript_access_and_secret_boundary`;
- `kora plugins list` and `kora conformance`, and the installed third-party provider fixture;
- the `kora doctor` leg of `test_cel_evaluating_entry_points_refuse_without_binding`;
- the graph-version retirement leg of `test_admission_limits_are_atomic`.

## 8. Observability, threat-model evidence and measurements

The VcsRef contract adds evidence obligations for frozen schema/behavior identity (TM-04/TM-12), trusted publication facts and exact result replay (TM-05/TM-08/TM-11), summary authorization and owner-specific retention (TM-07), bounded decoding, validation and upload cleanup (TM-10), and authenticated observation/stale-result fencing (TM-02/TM-09). It changes no credential authority, threat actor, execution isolation claim or logging owner. Artifact-model checks activate with work item 2; provider/runtime and API integration checks activate with their component implementations. Validation/digest/summary measurements cover peak and retained allocations across accepted and refused bounded payloads; upload measurements additionally cover concurrent staging, refusal, cancellation, recovery and reservation release. Numerical evidence is collected on an uncontended runner under the existing performance profile. The specification supplies no performance or leak-freedom result.

**Observability activation rows** ([observability.md section 7](../knowledge/specs/observability.md#7-component-coverage-and-activation)). A row activates with the first implementation of its component:

| Row | Work item |
|---|---|
| Logging infrastructure, the shared bootstrap and the common tests | 1 |
| Provider registry | 6, under [issue #150](https://github.com/realkarych/kora/issues/150) |
| DSL/compiler/IR | 2, 3; the compiler part is pending: DSL |
| Metadata/migrations/retention | 5 |
| Runtime, harness adapter and VCS provider rows | 7 for the fakes; 13 and 14 for real VCS/runtime; 15 adds independent evidence for both `claude` and `codex` |
| Authentication, artifact storage, artifact type and approval channel provider rows | 8 |
| Scheduler/admission/retries | 10 |
| API and webhooks; shared clients/CLI/MCP | 11 |
| Runtime supervisor/common executor | 13 for the common executor, 14 for the runner |
| Secrets provider row | 14 |
| Session runner/harness bridge | 14, 15 for both `claude` and `codex`; each adapter proves safe normalisation, correlation, redaction, stream separation and bounded failure behavior |
| Policy provider row | 14 |
| Sensors and observation dispatcher | 16 |
| Worker | 17 |
| Startup/doctor/deployment | 20 |
| Browser; product harness instructions; sensor and observability-exporter provider rows | pending: the web console, skills, sensors and the exporter |

**Threat-model evidence** ([threat model](threat-model.md#verification-and-residual-limitations)):

| Threats | Evidence and work item |
|---|---|
| TM-01, TM-02 | Denied access and service and user separation: 11 (route-authorization coverage), 12, 17, 18 and 20 (tokens). Authentication and approval-channel conformance: 8 and 16. Authorization during the run-read wait: 19. Pending: the session, CSRF, webhook and mounted-MCP cases (the web console, webhooks, mounted MCP). Worker route contracts of [issue #139](https://github.com/realkarych/kora/issues/139): `test_worker_write_requires_its_grant` 11 and 17, `test_upload_requires_grant_before_bytes` 11 and 12. |
| TM-03, TM-04 | `test_required_policy_is_enforced_or_rejected` and `test_harness_launcher_preserves_control_boundary`: 14 and 15; the `claude` scripted-model cases and the `codex` real app-server deterministic-driver cases: 15. Each real policy matrix covers every declared call kind and supported permission/sandbox mode, checks the side effect, namespace-deny precedence, rejection of unsupported namespace kinds (`IR019`), repository-hook exclusion in hermetic mode and protected paths, preserving the `trust_repo_config: true` residual limits of ADR-0009. Closed type contracts (`IR022`): 2 and 3. Expression bounds and the CEL function restriction: 4. The template sandbox: 17. Pending: the seeded/changed namespace-policy legs (#140), the compiler leg of the closed type contracts (DSL). Protocol model contracts of [issue #134](https://github.com/realkarych/kora/issues/134): `test_tool_policy_layers` and `test_policy_failure_denies` 14, `test_adapter_exception_order` 15, `test_runner_outcome_covers_every_stop` 14 and 15. |
| TM-05, TM-11 | `test_candidate_git_configuration_cannot_execute_in_publisher`, `test_publication_destination_is_trusted`, `test_publication_preserves_candidate_commits` and the local legs of `test_publication_sealing_rejects_mutable_aliases`: 13, 14 and 17. `test_claude_session_receives_only_constructed_environment` and `test_codex_session_receives_only_constructed_environment`: 15. Stored references and the worker's artifact upload: 8 and 17. Pending: `test_candidate_cannot_forge_runner_journal` and `test_agent_descendants_cannot_access_publication_credentials` (Docker); late authorization and the publication-credential legs (code host); target-source upload replay (target). Protocol model contracts of [issue #134](https://github.com/realkarych/kora/issues/134): `test_adapter_cannot_emit_supervisor_kinds` 14, `test_handle_and_events_are_secret_and_capability_free` 14 and 15, `test_runtime_observation_is_not_quiescence` 9 and 17. Local runtime contracts of [issue #123](https://github.com/realkarych/kora/issues/123): `test_control_channel_rejects_impostors`, `test_journal_classes_and_caps`, `test_transfer_digest_mismatch_is_rejected`, `test_receipt_names_its_scope`, `test_reused_pgid_is_never_signalled`, `test_runner_environment_excludes_worker_credentials` and `test_state_root_and_socket_dir_refuse_unsafe_ownership` 14; `test_launcher_takes_spec_from_private_channel` 14 and 15. |
| TM-06, TM-07 | `test_claude_session_receives_only_constructed_environment` and `test_codex_session_receives_only_constructed_environment` on local: 15. `IR007`: 3. Secret-prefix rejection and redaction: 14. Retained-projection authorization: 12. Pending: the Docker leg and runtime network-capability conformance (Docker), sensor destinations (starts), payload expiry (the retention task, #140) and the browser checks (web console). Protocol model contracts of [issue #134](https://github.com/realkarych/kora/issues/134): `test_secret_value_never_rendered` and `test_secret_reveal_is_confined` 6, `test_secret_registration_lifetime` 14, `test_event_content_is_bounded_and_classified` 14 and 15. |
| TM-08, TM-09 | Stale approvals, changed digests and the CEL approval cases on the API path: 9 and 18. Unknown execution and dispatcher closure: 9, 14 and 17. Pending: replaced waits and late results that need retry or wait events, and the channel-path legs (code host). Worker route contracts of [issue #139](https://github.com/realkarych/kora/issues/139): `test_successful_attempt_requires_closure_evidence` 9, 10 and 17; `test_success_pending_completion_is_rechecked` 9 and 10; `test_terminal_report_rejects_unbound_evidence` 9 and 11; `test_recovery_relay_closes_earlier_grants` and `test_adopted_attempt_reports_after_earlier_grants_close` 9 and 17; relayed runtime and dispatcher evidence is trusted only as far as the current lease holder. Local runtime contracts of [issue #123](https://github.com/realkarych/kora/issues/123): `test_dead_worker_dispatcher_closed_by_process_exit`, `test_live_old_worker_keeps_adoption_observational` and `test_worker_child_effect_keeps_dispatcher_open` 14 and 17; `test_unconnected_grant_closed_by_process_exit` 9, 14 and 17; `test_process_exit_evidence_requires_same_host_and_pid_namespace` 14; `test_stale_incarnation_claim_binds_its_own_process` 9 and 17. The default-form, custom-predicate and fixed-response-schema cases of #137 belong to 2–4, 9 and 18; `test_reject_reports_authoritative_status` belongs to 18. Missing or invalid decisions cannot acquire a default response value. Existing authority, stale-content and CEL-error cases remain required; code-host/head-observation legs activate in 16 and 18. |
| TM-10 | Admission races on both databases: 9. Deadline enforcement: 10, 13, 14 and 17. Bounded malformed inputs and provider results: `test_claude_stream_volume_is_bounded` and `test_codex_stream_volume_is_bounded` (15) and the external observation gates (13, 14). The bounded run-read wait: 19. Protocol model contracts of [issue #134](https://github.com/realkarych/kora/issues/134): `test_command_output_volume_is_bounded` 13 and 14, `test_finalise_memory_is_bounded` 7 and 15, `test_journal_backpressure_never_blocks` 14 and 15. Worker route contracts of [issue #139](https://github.com/realkarych/kora/issues/139): the cap and reservation legs of `test_upload_requires_grant_before_bytes` 11 and 12, and `test_refused_upload_bytes_are_reclaimed` 5 and 12. |
| TM-12 | `test_claude_binary_digest_mismatch_prevents_session_start` and `test_codex_binary_digest_mismatch_prevents_session_start`: 15; each verifies its own installed version and binary identity. Installed-provider discovery: 6; its third-party conformance leg is in the #140 list. The CEL binding's packaging cases and the scheduled scan of its bundled crates: 4; its entry-point cases: 4, 10, 11 and 20. Dependency, secret and static-analysis scanning in CI: 1. What a serving process may select ([fakes.md section 2](../knowledge/specs/fakes.md#2-fakes-never-reach-a-serving-process)): `test_serving_process_refuses_fake_providers` 6, 7, 17 and 20; `test_fake_import_rule` active since #136; `test_production_runner_refuses_fake` 14; `test_fake_results_carry_fake_evidence` 7 and 17; `test_dry_run_resolves_no_secret` 7 and 20, its dry-run leg pending #140. `test_session_spec_is_closed` of [issue #134](https://github.com/realkarych/kora/issues/134): 15 and 17. |

**Measurements:**

| Measurement | Work item |
|---|---|
| The run-read wait measurements of [api-and-agent-surface.md](../knowledge/specs/api-and-agent-surface.md#bounded-run-read-wait-adr-0002); their process-exit leg runs once work item 20 exists | 19; the retry, fan-out and mounted-MCP legs are pending |
| `test_claude_stream_volume_is_bounded` and `test_claude_session_releases_resources` | 15, Claude evidence only |
| `test_codex_stream_volume_is_bounded` and `test_codex_session_releases_resources` | 15, separate Codex evidence through real app-server |
| CEL compact 100 ms regression and provisional estimate-golden/specification checks ([ir.md section 7](../knowledge/specs/ir.md#7-expressions-and-templates)); full fitting, RSS/GIL calibration and crash-threshold searches are removed under the scoped exclusion | 4, under #167 |
| CEL lifetime: repeated compile and evaluate cycles with cancellation and shutdown, and the compiled-program cache over its bound | 4 |
| CEL validation of a 1,000-node IR and of a maximal `/validate` body | 3, 11 |
| CEL lifetime: metadata growth and listing latency on both databases | 5, 12 |
| CEL under load: approval and start latency while validation runs | 18 |
| CEL in standalone: event-loop lag and start and stop cycles | 20 |
| CEL under the scheduler's evaluation allowance, many waits, `event_evaluation` growth, the projection of a 4 MB event, fan-out and `kora test` cycles | pending: branch, retry, `wait_event`, fan-out, DSL |
| NFR-3.1 scheduling latency: the scheduler part | 10 |
| NFR-3.1 scheduling latency: end to end | 21 |
| Logging overhead and bounded output ([observability.md section 8](../knowledge/specs/observability.md#8-future-executable-evidence)) | 1 |
| Event-sink back-pressure against policy-callback, interrupt and SDK-reader latency, including callback-emitted records and a stuck writer ([issue #134](https://github.com/realkarych/kora/issues/134)) | 14 with the fake adapter, 15 with Claude |
| Redaction matching cost per record under the stream-volume workload ([issue #134](https://github.com/realkarych/kora/issues/134)) | 14 |
| Finalisation memory at the collection caps and the 100,000-event ceiling, and memory during the writer-health window ([issue #134](https://github.com/realkarych/kora/issues/134)) | 7, 15 |
| Command output volume and on-disk log bytes ([issue #134](https://github.com/realkarych/kora/issues/134)) | 13, 14 |
| Redaction registration count, bytes and CPU across rotation, reacquisition, API issuance and adoption ([issue #134](https://github.com/realkarych/kora/issues/134)) | 14 |
| Counter write frequency per heartbeat ([issue #134](https://github.com/realkarych/kora/issues/134)) | 17 |
| Journal fsync and group-commit cost on macOS ([issue #134](https://github.com/realkarych/kora/issues/134)) | 14 |
| NFR-3.2 relay latency ([issue #134](https://github.com/realkarych/kora/issues/134)) | 15, 17 |
| Upload RSS while streaming the largest part, and disk use at the caps ([issue #139](https://github.com/realkarych/kora/issues/139)) | 11, 12 |
| Cost of a replayed and of a conflicting segment chunk ([issue #139](https://github.com/realkarych/kora/issues/139)) | 12 |
| Release cycles for stored objects and bytes on success, refusal, stale epoch, disconnect, cancellation and shutdown, with the per-worker staging counter back at baseline ([issue #139](https://github.com/realkarych/kora/issues/139)) | 5, 12 |
| Run-lock hold per report type (heartbeat, heartbeat with evidence, segment, artifact, usage, terminal report with N grants), feeding the scheduler.md section 12 hot-run measurement at the declared flush policy ([issue #139](https://github.com/realkarych/kora/issues/139)) | 9, 10 |
| Writes per heartbeat (attempt, worker, marker) and per segment chunk (the staging row and the accepting transaction, two writer transactions on SQLite) ([issue #139](https://github.com/realkarych/kora/issues/139)) | 9, 12 |
| Cost of retention reclaiming staging rows ([issue #139](https://github.com/realkarych/kora/issues/139)) | 12 |
| The re-read pass computing `payload_digest` at `final` for the largest artifact ([issue #139](https://github.com/realkarych/kora/issues/139)) | 12 |
| Claim response size at the largest resolved config and inputs ([issue #139](https://github.com/realkarych/kora/issues/139)) | 11, 17 |
| NFR-3.2 relay latency at the flush policy ([issue #139](https://github.com/realkarych/kora/issues/139)) | 17 |
| Runner spawn latency and resident memory per runner, at 1 and 10 concurrent attempts ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Launcher overhead per harness launch ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14, 15 |
| Journal cost by durability class on Linux and macOS, and adapter drain latency under slow supervisor syncs ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Adoption latency, also with a journal near its cap, and `events{from}` resume cost with the index disabled near the cap ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14, 17 |
| Runner memory over a long session up to the 100,000-event ceiling, and peaks during transfer and `finalize` ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Registry lock contention at 10 concurrent submits and retires, and with a stopped holder; peer-attestation cost per accepted connection under connection churn ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Transfer throughput at 64 MiB in both directions ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Retirement and normal-completion stop latency, with a cooperative and with a `SIGTERM`-ignoring candidate ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Runner behaviour on a full disk, with the runner present and with the worker-side fallback: temporary registry and checkpoint files and their bytes across repeated failed `retire` cycles, journal size once the journal is closed, the fallback's one sweep and its CPU per retried write, worker shutdown with the fallback's retry pending, how long the runner, its socket, connections and held stop evidence stay while the tombstone cannot be written, the bytes of held stop evidence, the retry rate and the runner's CPU during the stall, with `ENOSPC` and `EDQUOT`, and recovery once space returns ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Local runtime resource lifetime: descriptors, processes, zombies (including one registered zombie held while other children exit, with the CPU idle and the others reaped), directories, sockets, connections and followers, and also process memory (Python heap, RSS and native), threads and timers (the writer, the reaper, the batch timer, the socket re-verification timer, the runner's full-disk retry timer and the worker-side fallback's retry backoff) and the worker's spool files and directories, as peak, post-quiescence and per-cycle growth in one long-lived worker, across success, cancel, retire, deadline expiry, worker shutdown, runner loss, worker-restart adoption, connection churn, a late-registering runner, an aborted transfer, `transfer_out` retry exhaustion, `ENOSPC`, `journal_unavailable`, `kill()`, launcher death and `release`; retained bytes per tombstone and lock file ([issue #123](https://github.com/realkarych/kora/issues/123)) | 14 |
| Fake runtime and harness lifecycle: repeated cycles of success, error, a worker loss at each point, a runner death in each phase, cancellation, `lose_registry`, a watchdog expiry, a `scenario_real_time_limit` expiry, `close()` with an attempt in flight or a `holds_time: true` barrier held, and the testing runner on `local`, in one process; peak, post-quiescence and per-cycle growth of memory, and the counts of tasks, threads, timers, descriptors, subprocesses, process groups and temporary directories sampled after an explicit cleanup condition; workload, cycle counts and budgets declared before the run, separate process runs, and a base and head comparison ([fakes.md section 10](../knowledge/specs/fakes.md#10-contracts-and-measurements)) ([issue #136](https://github.com/realkarych/kora/issues/136)) | 7; the worker leg in 17 |
| Virtual-time cost: advances and wall time per scenario and integration test, on both databases ([issue #136](https://github.com/realkarych/kora/issues/136)) | 7, 10, 20 |
| The share of fake-backed suites in CI wall time against the 10-minute budget ([issue #136](https://github.com/realkarych/kora/issues/136)) | 7, and each work item adding such suites |

Both adapters' executable evidence remains **pending implementation**. Work item 15 opens an owning issue for each adapter before code and records its named cases, workloads, bounds and artifacts; work item 21 owns their combined acceptance gate. Claude probe results do not establish Codex behavior. Neither missing tooling nor an unobserved hook is a passing check.

For Codex, the stream-volume and lifetime measurements cover the app-server process, stdio buffers and descriptors, RPC reader/writer tasks, queues and pending requests, managed policy-hook/MCP/helper subprocesses, and per-attempt configuration, temporary directories, workspaces and transcript handles. Repeat successful and failed sessions, failed initialization, cancellation, an unacknowledged interrupt followed by runtime escalation, worker-restart adoption and shutdown, including slow or broken output. Measure peak and settled memory, tasks, descriptors and subprocesses after an explicit quiescence condition; verify release of per-session resources and no unexplained retained growth across cycles while preserving required durable recovery evidence. The same lifecycle method applies independently to Claude. Measurements retain existing protocol/output bounds and the ten-minute CI budget; these amendments neither supply observed results nor relax limits.
