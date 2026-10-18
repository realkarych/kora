# kora MVP design — the YAML intermediate representation (IR)

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: the canonical graph representation that `kora compile` emits, `kora validate` checks and the scheduler consumes (FR-1.2–1.7, FR-2, FR-3.2, FR-6.3–6.6, NFR-6). The machine-readable contract is [ir.schema.json](ir.schema.json), published at a stable `$schema` URL and registered in SchemaStore so editors validate `*.ir.yaml` files; worked examples are in [examples/](examples/). Design decisions taken by the owner on the choices the requirements left open are recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md) and referenced here as **DD-n** ("design decision n").

## 1. Design rules

1. **The IR is data, not code.** The scheduler never imports user Python; everything it needs is materialised in the file, including resolved per-node configuration (FR-3.2) and expressions for branches and predicates (section 7).
2. **Stable string ids and named references**, never positional edges (FR-1.5). Node ids are unique within a graph document and match `^[a-z][a-z0-9_]{0,63}$`.
3. **Three concerns in three sections** (FR-1.3): `graph` (topology and node definitions), `runtime` (graph-level runtime and policy configuration), `secrets` (references only, never values). Compiler provenance (preset and defaults snapshots) lives under `meta`, outside all three.
4. **Lenient at the root, strict inside typed objects** (FR-1.5): unknown root keys are ignored with a warning; unknown keys inside node, retry, limits and config objects are errors. `x-` prefixed keys are allowed anywhere for tool metadata.
5. **Deterministic output** (NFR-6.2): keys in schema order, lists in source order, no timestamps.
6. **Two versions, kept apart.** `ir_version` at the root is the format version under semver (NFR-6.3: minor bumps add optional fields, major bumps may remove or re-type fields); `graph.version` is the author's own version string for the graph, informational, distinct from the control plane's deployed version numbers.

## 2. Document layout

```yaml
$schema: "https://kora.dev/schemas/ir/1.0.json"
ir_version: "1.0"             # format version
meta:                         # written by the compiler, informational; the scheduler never reads it
  compiler: "kora 0.1.0"
  source_digest: "sha256:…"   # digest of the Python sources that produced this file
  presets: { <preset_id>: { … } }     # snapshot of each preset as used at compile time, for diffing and provenance
  defaults: { … global and graph layers … }
type_contracts:
  VcsRef: { builtin_revision: "1" }
  StructuredOutput: { builtin_revision: "1" }
graph:                        # section (a): topology
  id: sql_optimization        # graph id, stable across versions
  version: "3"                # the author's own graph version, informational
  namespace: dwh_core
  description: "…"
  params: { …JSON Schema… }   # run parameters accepted by triggers
  triggers: [ … ]             # section 8
  overlap: allow              # allow | skip | queue (DD-12); when absent: queue for webhook-fired runs, allow for every other trigger kind
  target:                     # optional (FR-6.11, ADR-0014): the business target a run works on and the per-target policy, section 8
    key: { from: find_target, output: target, path: task_id }   # or { param: target_task }: a field of a node output, or a run parameter
    on_active: defer          # allow | defer; when absent: allow
  nodes: { <node_id>: <Node> }
  subgraphs: { <subgraph_id>: <Subgraph> }   # reusable bodies for subgraph and fan_out nodes
runtime:                      # section (b): policy and infrastructure
  runtime: docker             # runtime protocol implementation, by name
  vcs: git
  artifact_storage: s3        # artifact storage implementation: local (standalone, development) | s3 (any S3-compatible service) in MVP (FR-4.4)
  approval_channels: { vcs: { provider: github, accept_merged_as_approval: false, feedback_from: collaborators_write }, cli: {} }   # DD-35
  concurrency: { max_running_nodes: 20 }
  limits: { timeout: "72h", budget_usd: 200 }      # whole run
  stall_after: "15m"          # runtime-level default for agent-loop silence; nodes override in limits (DD-15)
  retention: { transcripts: "30d", artifact_payloads: "30d" }   # graph-level, namespace default applies when absent (DD-4)
  protected_paths: ["Makefile", "tests/**", "tools/gates/**"]     # gate files no agent node may edit; verified before every command gate (DD-34)
secrets:                      # section (c): references only
  aliases: { SERVICE_TOKEN: "secret://env/SERVICE_TOKEN" }   # every reference must fall inside the prefixes the namespace is allowed (IR007)
```

`meta.presets` and `meta.defaults` are **informational snapshots**: the scheduler reads only each node's resolved `config` (FR-3.2); they let a reviewer diff a preset change and a UI show provenance.

### 2.1. Authoritative type contracts (ADR-0018)

The unreleased IR 1.0 uses `kora.execution.v1` interpretation. `type_contracts` is required in this format and contains every used non-list type and its parent/reference closure. A graph that uses no artifact types, such as a timer-only graph, has an empty `type_contracts: {}` closure. `List[T]` derives its contract from the fixed list semantics and `T`; it has no separate registry alias. The compiler emits definitions from author-side models, including an explicit `$schema`, and installed immutable built-in revisions. It performs no registration/network write during compilation.

A built-in entry is `{builtin_revision: "1"}`. The installation must resolve that revision to its immutable schema and behavior descriptors; a revision cannot be rebound to new bytes. A custom entry contains `schema`, `parent` (a type name or null) and `behavior` with exact `validation`, `digest_fields` and `summary` implementation identities. These identities identify provider-declared immutable semantics, not executable source or an instruction to download/import author code. Registered provider metadata supplies the installed binding and verifies its implementation identity; an unavailable or mismatched binding is rejected, not replaced by the current implementation.

The generic behavior identities are `kora.json-schema-2020-12.v1` for schema validation and `kora.json-payload.v1` for whole-payload digest selection and the identity summary. Custom summary bindings use their provider's immutable identity and the trusted installation's configured entry point. Identity summaries are content-bearing payloads, not permanent small metadata. Actual selected fields/projections and summaries are stored at production time ([protocols.md](protocols.md) section 8).

Schemas use JSON Schema draft 2020-12. Local fragment references resolve inside their document; cross-type references use `urn:kora:artifact:<name>` with a namespace separator encoded as `:`, for example `urn:kora:artifact:dwh_core:Candidate`. Built-in schemas have the same canonical `$id` form. Deployment builds an in-memory resolver only from the saved closure. Other external references, unresolved parents/references, parent cycles, unsupported dialects and unavailable behavior identities are `IR022` at the affected path. No runtime network resolver or mutable registry lookup is allowed. Schema syntax/metaschema validity and closure/assignability are semantic checks beyond the outer JSON Schema.

Custom definitions in the document are authoritative, even if the namespace's current alias has changed since compilation. Deploy checks namespace ownership and the whole closure, expands built-in revisions and includes inline node output schemas in `graph_version.execution_contract`. It does not replace an embedded definition with today's registry value. Registration changes a namespace alias for future authoring only. Deployment identity is the digest of canonical `{ir, execution_contract}`; `ir_digest` remains the digest of the compiled document. The same complete deployment is idempotent; different resolved resource/type behavior cannot reuse the old identity. `meta` remains informational.

Every consumer uses this closure: schema-driven CEL checking, edge assignability, worker output schema, API upload validation, branch/collection evaluation and content-bound approval digests. Scheduler-only nodes acquire no physical attempt row. A supported execution-semantics version must implement both saved built-in revisions and declared custom behavior; unsupported execution fails explicitly before the affected operation, while historical reads use saved results. Referenced skill/MCP/resource identities are carried when declared or observed, with delivery still owned by issues #45 and #48.

## 3. Nodes

Every node has the common envelope below; `config` is kind-specific (section 4).

```yaml
nodes:
  implement:
    kind: agent
    description: "Implement one hypothesis on an isolated checkout"
    source: { file: "graphs/sql.py", line: 23 }        # NFR-10.3
    inputs:
      hypothesis: { type: Hypothesis, from: $inputs, path: hypothesis }   # inside a fan-out body: the mapped element
      previous_attempt: { type: PreviousAttempt, from: $previous_attempt, optional: true }
    outputs:
      change: { type: VcsRef }
      session: { type: AgentSession }                   # implicit for agent nodes, listed for clarity
    depends_on: []                                      # control-only edges (no artifact)
    trigger_rule: all_success
    retry: { … }                                        # section 6
    limits: { timeout: "2h", max_events: 200, budget_usd: 15, stall_after: "20m" }
    attempt_invariant: false                            # FR-2.7a memoisation flag
    independent_of: []                                  # inputs whose new versions do not invalidate this node (FR-6.8a)
    protected_paths: []                                 # node-level addition to runtime.protected_paths (DD-34)
    config: { … }
```

**Edges** are the union of `inputs[*].from` and `depends_on`. An input names the producer node and one of its declared outputs; the validator checks that the producer's output type is assignable to the input type (the same saved type contract, or a subtype through the graph version's saved parent closure, FR-4.3 and ADR-0018). Reserved sources: `$params` (run parameters), `$previous_attempt` (section 5), and `$inputs` inside a subgraph body (the subgraph's declared inputs, by `path`; in a fan-out body the mapped input resolves to the current list element). There is exactly one way to feed a body: the `inputs` of the `subgraph` or `fan_out` node. Inputs declared with `optional: true` may be unresolved when the producer was skipped; others make the node `upstream_failed`/`skipped` according to the trigger rule.

**Input independence** (FR-6.8a, ADR-0007): `independent_of` contains names from this node's `inputs`, not producer IDs. Unknown names fail semantic validation with `IR023`. It preserves only an already successful outcome on the affected input edge; any other affected input or control edge still invalidates the node. Consumers without a successful result are reevaluated. Runtime invalidation resolves declared edges even before input binding, through body input forwarding and exports/collection, together with current invariant-reuse bindings ([data-model section 6](data-model.md#6-clear-downstream-fr-68a)). It never waives a branch/control gate.

**Outputs** declare names resolved exclusively through the compiled `type_contracts` and the deployed closure, not current registry aliases (ADR-0018). Built-in names: `StructuredOutput`, `VcsRef`, `AgentSession`, `CommandResult`, `ReviewBundle`, plus `List[<type>]` for fan-out results and user types registered under `namespace/Name`. A `StructuredOutput` carries its JSON Schema inline under `schema`. **Agent output contract** (DD-36): an agent node declares at most one `StructuredOutput`-derived output and at most one `VcsRef` output besides the implicit `session`; `output_schema` is copied into the artifact's schema; the worker publishes the workspace only when a `VcsRef` output is declared, and a declared `VcsRef` with no diff fails with `artifact_invalid`. The `AgentSession` payload carries harness name and installed version, session id, transcript `storage_ref`, usage per model with completeness, cost estimate with basis and billing class, turn count, duration, `permission_denied` events, stop reason, and provenance (worker id, image digest). A `CommandResult` payload carries the exit code, the duration, stdout and stderr references with truncated tails, and the shielding of the measurement (DD-34; [architecture critique of 2026-10-16, finding A04](../reviews/architecture-critique-2026-10-16.md#a04-evaluator-independence)): `protected_paths`, the resolved list in force for the node, `base_commit`, the commit the digests were compared against, and `protection`, `verified` when every listed path was unchanged against `base_commit` before the command ran and `unprotected` when the node declares no protected paths; a changed protected path fails the attempt with `protected_path_modified` before any `CommandResult` exists. All of them enter the `ReviewBundle` facts.

A **`ReviewBundle`** (DD-33) has two parts: `facts`, written by kora from its own records and read-only to agents (every input artifact of the bundle node with type, digest, producing attempt, worker and image; gate results from `CommandResult` inputs with exit codes, protected paths, base commit and verification result, so that each gate is labelled as verified against its protected paths or unprotected; metrics references; the `VcsRef` with commit; the run's attempt cost), and `narrative`, the only part the agent node writes (`summary`, `rationale`). Approvals bind to the digests of `facts`; `kora web` and the change-request comment render the two parts visibly apart, each gate with its label. There is no node kind for building artifacts (DD-8, ADR-0012): packing a `Candidate` or selecting the best candidate by a fixed formula is a command node over its materialised inputs (section 4), because a copy and a formula need no model and must be reproducible; writing the `ReviewBundle` narrative, or a selection that needs judgement, is an ordinary agent node on a cheap model from its typed inputs. kora itself attaches the aggregated cost of all attempts to the approval request (section 4.2); the agent never computes cost.

**Trigger rules** (FR-1.6 plus DD-1): `all_success` (default), `all_done`, `one_success`, `one_failed`, `none_failed`, `all_skipped`, and `none_failed_min_one_success` (borrowed from Archon: no upstream failed and at least one succeeded, so a node after an all-skipped branch stays skipped). The enum is closed in IR v1.

## 4. Node kinds and their `config`

| kind | purpose | required config | produces |
|---|---|---|---|
| `agent` | one harness session | `harness`, `prompt`, resolved harness config (section 4.1) | declared outputs + `AgentSession` |
| `command` | deterministic gate | `cmd` (argv, no shell), optional `output_file`, `env`, `workspace`, `success_exit_codes` (default `[0]`) | `CommandResult` (exit code, duration, stdout and stderr references, truncated tails, `protected_paths`, `base_commit` and `protection`, section 3) and, with `output_file`, one declared output of type `StructuredOutput` or any custom artifact type whose schema the file must satisfy; stdout and stderr lines stream as session events (DD-36) |
| `human_input` | approval gate | `channel`, `form`, `timeout`, `on_expiry`, `binds` | `StructuredOutput` (form response) |
| `wait_event` | sensor | `event` or `poll`, `interval`, `timeout`, `on_expiry` | `StructuredOutput` (event payload) |
| `timer` | durable delay | `duration` or `at` (absolute time) | none |
| `branch` | deterministic routing | `cases: [{when, to}]`, `else` | none; durable selection gates its direct targets (section 4.5) |
| `fan_out` | map a list over a subgraph | `body` (subgraph id), `over` (name of the list-typed input), `max_parallel`, `skip_failures` | per-item instances; the node's own outputs are `List[<type>]` collected from the body's outputs (`collect`) |
| `subgraph` | graph as a node | `body` (subgraph id); the node's `inputs` feed the body | the body's declared outputs |

**Command inputs.** Before a command starts, the worker writes the payload of each of the node's inputs to `.kora/inputs/<input name>.json` in the workspace, a `List[...]` input as a JSON array of element payloads, so a script reads lists and nested artifacts from files; `cmd` elements may still carry scalar fields through templates (`{{ inputs.target.task_id }}`). `.kora/` is kora's own directory in every workspace, excluded from publication ([protocols.md](protocols.md) section 4).

### 4.1 Agent node config (resolved, FR-3.1)

```yaml
config:
  harness: claude                     # claude | codex in MVP; acp:<agent> reserved for the adapter after MVP (DD-27, ADR-0004); the harness version is fixed by the installation, not the node (DD-14)
  model: claude-opus-5-5
  prompt: "Implement hypothesis {{ inputs.hypothesis.title }} …"   # template over inputs and params
  system_prompt_append: "…"
  skills: [sql-optimizer]             # names; deploy resolves each to a content digest recorded on the graph version
  subagents: [ {name: …, description: …, prompt: …, tools: […]} ]
  hooks: {}                           # harness hook config, declared-only
  mcp_servers: { warehouse: { type: stdio, command: "warehouse-mcp", args: ["--cluster", "prod"], digest: "sha256:…" } }   # pinned by package or image digest like skills; agent nodes never get kora's own MCP server (DD-5)
  tool_policy: { allow: ["Read", "Edit", "Bash(etl *)", "Bash(git *)"], deny: ["Bash(rm -rf *)"] }   # the one tool-call policy, evaluated by the policy protocol at every tool call (DD-29)
  permission_mode: dont_ask           # kora vocabulary, mapped per harness
  sandbox: workspace_write            # kora vocabulary, mapped per harness
  trust_repo_config: false            # FR-3.4
  network_policy: { egress: ["api.anthropic.com", "warehouse.internal"] }   # FR-8.6; enforced by the runtime, harness sandbox is a second layer (DD-21)
  workspace: { repo: "…", rev: "{{ inputs.change.commit | default('main') }}" }
  output_schema: { … JSON Schema … }  # for the StructuredOutput output
  env: { ETL_ENV: dev }
  secret_env: { SERVICE_TOKEN: SERVICE_TOKEN }   # env var -> alias in secrets.aliases
  runtime: docker                     # per-node override of runtime.runtime (FR-8.4)
  vcs: git                            # per-node override (FR-5.3)
```

`harness` names a harness adapter registered with the installation through the `kora.provider` entry point (`claude` and `codex` in MVP, `fake` in tests). The schema already admits the form `acp:<agent>`, where the part after the colon selects the ACP-capable agent, so the ACP adapter that follows MVP needs no change to the graph format; until that adapter is installed, `acp:<agent>`, like any other name without an adapter, fails validation with `IR018`.

`workspace.from_ref` names an input path holding a `VcsRef`; when that artifact is present the checkout uses its recorded commit, otherwise `rev`, so a gate runs on exactly the commit the evidence names. A `fresh_with_feedback` attempt is the one path that checks out the branch of the previous attempt's `VcsRef` at its current head instead ([scheduler.md](scheduler.md) section 7). `permission_mode` values: `plan`, `accept_edits`, `dont_ask`, `bypass`. `sandbox` values: `read_only`, `workspace_write`, `full_access`. The harness mapping tables in [protocols.md](protocols.md) say which flag each becomes and which fields a harness cannot honour (compile-time warning, FR-3.3). Harness API keys reach the session as environment variables in MVP; a per-attempt credential proxy is the runtime capability `credential_proxy`, implemented post-MVP (DD-23, DD-30).

`tool_policy` is the input to the **policy protocol** ([protocols.md](protocols.md) section 12, DD-29) and the only tool-permission field in IR 1.0: the DSL's `allowed_tools` and `disallowed_tools` compile into it. The harness's pre-tool-call hook asks the session runner before every tool call; the runner evaluates the namespace's `tool_policy` (set by the platform team, prepended) and then the node's, and a deny from either stops the call and is recorded as a `permission_denied` session event on the `AgentSession`. Patterns use the harness tool-pattern syntax; argv-level matching rejects shell chaining metacharacters and `-c` or `--exec` style indirection inside an allow-listed command. The field is inherited through the preset layers. `Edit` and `Write` on `protected_paths` (DD-34) are denied for every agent node of the graph. MVP ships the `allow_deny` implementation; Cedar or OPA arrive as plugins without an IR change.

**Protected paths** (DD-34): `runtime.protected_paths` plus the node's `protected_paths` list gate scripts, build files, test directories and tooling configuration. Before a command gate runs, the preparation phase of its attempt compares the paths changed between the base commit and the candidate commit (the `changed_paths` capture of the checkout plan, [protocols.md](protocols.md) section 4) with those paths and fails the attempt with class `protected_path_modified` on a match; no second checkout is made.

### 4.2 Human-input node config (FR-2.2, FR-7)

```yaml
config:
  channel: vcs                         # name in runtime.approval_channels
  form: { type: object, properties: { decision: {enum: [approve, reject]}, comments: {type: string} }, required: [decision] }
  timeout: "3d"
  on_expiry: fail                      # fail | skip | default_value
  default_value: null
  binds:                               # content binding, FR-7.2a; a push during review invalidates the request and fails the node with candidate_moved
    vcs_ref: inputs.bundle.facts.change  # input path whose commit SHA the approval covers
    gate_digests: [inputs.bundle.facts]  # input paths whose digests the approval covers; defaults to the ReviewBundle facts when the context is a bundle
  approve_when: "response.decision == 'approve'"   # CEL expression that makes the node succeed; otherwise it fails with class approval_rejected
```

kora attaches to every approval request the run's aggregated estimated cost so far (the sum of usage records over the run at request time, [data-model.md](data-model.md) section 5), so the approver sees it next to the form without any node computing it (DD-8). Expiry is a `wait` row like every other timed wait (`timeout` becomes `wake_at`).

For the `vcs` channel (DD-35) the approval counts only when the approver is neither the change-request author nor kora's own bot identity and, when the namespace sets `approver_allowlist`, is on that list; "merged" is a separate opt-in signal (`accept_merged_as_approval` in the channel configuration, default false). Feedback comments passed to `fresh_with_feedback` carry author and association, filtered to collaborators with write access by default (`feedback_from: collaborators_write`).

A push to the branch of the bound `VcsRef` while the request is `pending` moves the request to `invalidated` and fails the node with class `candidate_moved`, for every channel ([scheduler.md](scheduler.md) section 5; [architecture critique of 2026-10-16, finding A03](../reviews/architecture-critique-2026-10-16.md#a03-review-revision)). The request is never made again on the same bundle: `candidate_moved` is not retryable (section 6) and a manual retry of this node is refused with `API002`; the author retries the node that produced the bound `VcsRef`, with `fresh_with_feedback` to continue from the pushed commit, and the gates, the bundle and a new request follow through the normal downstream path. Before this node's downstream starts, the scheduler checks once more that the approved SHA is still the branch head.

### 4.3 Wait-event node config (FR-2.3)

```yaml
config:
  event: { type: webhook, name: deploy.finished, match: "payload.target == inputs.target.id" }
  # or: poll: { sensor: change_request_status, config: { ref: inputs.change }, until: "observation.status == 'merged'" }
  interval: "5m"
  timeout: "2d"
  on_expiry: fail
```

`poll.sensor` names an implementation of the **sensor protocol** ([protocols.md](protocols.md) section 11, DD-2); kora ships `change_request_status` and `ci_status` (through the VCS protocol) and `http` (GET a URL and expose the JSON body). `until` is a CEL expression over `observation`, the object the sensor returns. The `http` sensor only reaches URLs on the namespace's allowlist and never link-local or cloud-metadata addresses. Adopters add sensors through the `kora.provider` entry point without changing the IR; an invalid `config` is `IR014`.

### 4.4 Fan-out and subgraph

```yaml
candidates:
  kind: fan_out
  inputs:
    hypothesis: { type: "List[Hypothesis]", from: hypotheses, output: result }   # the mapped input, named by `over`
    baseline:   { type: MetricsReport, from: baseline, output: metrics }         # bound to every instance unchanged
  outputs:
    candidates: { type: "List[Candidate]", collect: candidate }                   # collected from the body's `candidate` output
  config: { body: attempt, over: hypothesis, max_parallel: 3, skip_failures: true }
```

A fan-out materialises one **instance** of the body subgraph per element of the `over` input at run time (NFR-3.3), node ids suffixed `[i]`; inside the body, `$inputs.hypothesis` is the element, every other input is the bound value. The fan-out node's own outputs are lists collected from the body outputs named by `collect`; there is no separate fan-in node. `skip_failures: true` collects the successful instances and succeeds when at least one succeeded; `false` fails on the first failed instance. An empty `over` list materialises no instance, and the fan-out succeeds with empty lists whatever `skip_failures` says; a non-empty list in which every instance failed still fails. A consumer of the empty list decides what an empty input means. Nested fan-out inside a body is allowed (NFR-1.4). A `subgraph` node has the same shape without `over`, `max_parallel` and `skip_failures`, and its outputs are the body's outputs unwrapped.

A `subgraphs` entry has the same shape as `graph` minus `triggers`, plus `inputs` and `outputs` declarations and its own `retry` and `limits`, which apply to the body as a unit (FR-2.7).

### 4.5 Branch ownership and selection

Every ordinary target named by any branch case or `else` must exist in that graph/body scope and have the branch ID directly in its `depends_on`. DSL lowering inserts the edge; direct IR validation requires it (`IR010`). A direct target has exactly one owning branch in MVP; listing it in multiple cases of that same branch is allowed. Multiple distinct owners are `IR010`. Ordinary DAG validation (`IR001`) includes these edges, rejecting self-targets and routing cycles. `$fail_attempt` is a reserved subgraph action and creates no node/control edge.

MVP rejects `attempt_invariant: true` on every `branch` and on every ordinary direct target named by any case or `else` (`IR010`, at the offending node's flag). Apply this semantic rule in every graph/body scope, including nested definitions, regardless of retry count or which case may be selected. A branch that only routes to `$fail_attempt` is still a branch and cannot be invariant; the reserved action itself is not a node to validate. Omitted/false flags remain valid, and other nodes retain ordinary invariant reuse. The shared validator used by compile and deployment enforces the rule; no runtime cached-route mechanism is introduced.

Selection is an additional readiness gate, not a substitute for ordinary dependencies or trigger/input checks. Every evaluation uses the current concrete owner instance's compact persisted selection ([data-model section 5.1](data-model.md#51-current-branch-selection)). An unresolved owner blocks readiness; a skipped owner or a successful owner that excludes the target skips it; failed/upstream-failed owner produces `upstream_failed`. A selected target still waits for all ordinary upstreams to be terminal and must satisfy its trigger rule and required inputs. The owning branch's control edge participates normally: its success counts for `one_success` and prevents `all_skipped`. Input independence cannot remove this gate.

Only direct targets have this ownership constraint. Untargeted reconvergent joins and failure handlers retain ordinary trigger semantics; no recursive blanket skipping occurs. Target-only clear cannot erase a retained exclusion, and owner clear removes its old selection before reevaluation. No new IR field or hidden runtime edge category is introduced.

## 5. Sub-graph attempt semantics (FR-2.7a)

A loop is a subgraph with `retry.max_attempts > 1`. Rules materialised in the IR:

- A node inside the body can end the attempt with `fail_attempt`: a `branch` case whose `to` is the reserved target `$fail_attempt` with a `reason` expression. The attempt ends `failed` with error class `attempt_failed`.
- Sub-graph attempt N+1 receives the subgraph's inputs plus `$previous_attempt`, a built-in `PreviousAttempt` artifact: `{subgraph_attempt: N, reason, error_class, artifacts: {<node_id>.<output>: <artifact ref>}}`. A node receives it only by declaring an input with `from: $previous_attempt`, so the dependency is visible in the graph. In the control plane a sub-graph attempt is the `subgraph_attempt` integer on the node instances; the word "attempt" alone always means a runtime submission ([data-model.md](data-model.md) section 5).
- Nodes permitted by section 4.5 to use `attempt_invariant: true` are not re-run in sub-graph attempt N+1; their earlier artifact version is reused and lineage records the reuse.
- `limits` on the subgraph (timeout, budget) span all sub-graph attempts; `limits` on body nodes apply per node attempt. `retry.max_attempts` on the subgraph counts sub-graph attempts; body nodes keep their own node-level retries inside each.
- Artifacts of every sub-graph attempt are retained; the previous ones are marked `superseded` when a new sub-graph attempt starts (FR-4.5).
- VCS side effects of failed attempts are never touched (FR-2.7, FR-6.1b).

## 6. Retry policy and strategy (FR-6.3, FR-6.4)

```yaml
retry:
  max_attempts: 3
  initial_interval: "30s"
  backoff_coefficient: 2.0
  max_interval: "10m"
  retry_on: [harness_error, timeout, events_exhausted, stalled, artifact_invalid]
  non_retryable: [budget_exhausted, approval_rejected]
  predicate: "outputs.verdict.approved == false"     # optional extra condition on artifacts
  strategy: fresh_with_feedback                       # agent nodes only: fresh | resume | fresh_with_feedback
```

Error classes (closed enum, used by `retry_on`, `non_retryable` and the data model): `harness_error`, `command_failed`, `timeout`, `events_exhausted`, `budget_exhausted`, `artifact_invalid`, `attempt_failed`, `approval_rejected`, `approval_expired`, `candidate_moved`, `wait_expired`, `stalled`, `protected_path_modified`, `secret_leak`, `infrastructure`, `cancelled`, `config_error`. Execution uncertainty is not an error class or an IR retry option: lease expiry preserves the original non-terminal attempt and reservations. ADR-0017 blocks all new runtime admissions in its run while execution/publication recovery is unresolved. Recovery can settle by affirmative adoption of the same live execution or its authoritative immutable prepared/completed checkpoint once every earlier grant/operation is closed; direct replacement still requires runtime termination and every dispatcher/platform publication closure. Manual retry records clear/cancel intent while blocked; a linked rerun is refused with `API002` for any replacement-blocking condition. A separately triggered run follows the ordinary target policy. `infrastructure` alone never consumes **agent** attempts: it is retried under the separate `infrastructure_retry` policy at `runtime` level (NFR-2.4). `stalled`, `protected_path_modified` and `secret_leak` are ordinary classes decided by the node's policy. `config_error`, `cancelled` and `candidate_moved` are never retryable (Archon's "never retry config errors"; a retried approval node would request again on the bundle built for the old commit, [scheduler.md](scheduler.md) section 5).

`strategy: resume` requires the runtime capability `supports_resume`; when the runtime lacks it, when the session handle is gone or when the installed harness version changed, the attempt falls back to `fresh_with_feedback` and records `strategy_fallback` (FR-6.4). In MVP neither reference runtime declares it (`local` and `docker` release the workspace that holds the harness checkpoint, [protocols.md](protocols.md) section 2), so `kora validate` warns `IR009` for `strategy: resume` on them and every such retry takes the fallback; `fresh_with_feedback` is the strategy that works on every runtime and the documented default where the next attempt needs the previous attempt's work.

## 7. Expressions and templates

Branch conditions, retry predicates, `approve_when`, event `match` and sensor `until` are **CEL** (Common Expression Language) expressions (DD-3), evaluated by kora through the Rust-backed `common-expression-language` Python binding; `kora validate` runs its own schema-driven checker that types the `inputs.*` environment from the declared artifact schemas, and the CEL conformance suite runs in CI (the binding has no static checker of its own). Other SDKs use any conforming CEL implementation. The evaluation environment binds `inputs.<name>` (artifact payloads and summaries), `outputs.<name>`, `params`, `response` (approval form response), `payload` (inbound event) and `observation` (sensor result). Only CEL's standard functions are available; no custom functions and no user code.

**Paths** (`inputs[*].path`, `binds.*`, `workspace.from_ref`, `fan_out` and `human_input` references) are CEL member-access chains restricted to field selection and indexing, such as `inputs.bundle.facts.change` or `artifacts['implement.change']`; no operators, no calls. This is the one path notation in the IR. `timer` takes a `duration` or an absolute `at` time, never an expression.

Prompt templates (`prompt`, `system_prompt_append`, `cmd` elements) are **Jinja2 in a sandboxed environment** (DD-11): the full template language, the same variables as above, no filesystem loaders, no imports or includes, no access to Python attributes beyond the bound data. Templates are rendered once when the nonsecret execution request is sealed before preparation/execution effects; the attempt owns the saved rendered content and availability state. Recovery reads that content and never re-renders over current data (ADR-0018). Run parameters that arrive from a webhook may be used in prompts but never inside `cmd` elements or `workspace.rev` (`IR016`).

## 8. Triggers (FR-6.2)

```yaml
triggers:
  - { kind: manual }
  - { kind: cron, expr: "0 6 * * 1", timezone: Europe/Moscow }
  - { kind: webhook, name: optimize_target, auth: { kind: hmac, secret: WEBHOOK_SECRET }, params_from: payload }
  - { kind: webhook, name: optimize_from_ci, auth: { kind: bearer, secret: CI_TOKEN } }   # bearer deliveries also carry a timestamp and a nonce
  - { kind: sensor, poll: { sensor: http, config: { url: "…" }, until: "observation.body.ready == true" }, interval: "15m" }
  - { kind: graph, graph: upstream_graph, on: run_succeeded }
```

Every accepted start produces a pinned pending run or a durable no-run overlap-skip outcome, with an idempotency key (section 13 of [data-model.md](data-model.md), ADR-0020). There is no acknowledged unconverted trigger queue. Matching accepted replay precedes current graph/schema/target validation and quotas; full outstanding capacity is a structured retryable refusal, not an accepted delivery. Overlap handling is `graph.overlap`: `allow`, `skip` or `queue` (DD-12). When the field is absent, runs fired by a webhook trigger use `queue` and runs from every other trigger kind use `allow` (FR-6.2, ADR-0006). Skip considers accepted pending and running graph runs; queue promotion selects the oldest pending run under the graph lock only when no graph run is running. Save resolved overlap on each accepted run. Explicit allow and same-run retry retain their semantics. Cron fires forward only; there is no catch-up of missed fire times in v1 (DD-37). Webhook authentication is either an HMAC body signature in the Standard Webhooks shape or a bearer token with a timestamp and nonce inside a replay window, chosen per trigger; the secret is a reference into `secrets.aliases` (DD-17); the delivery's idempotency key is derived from signature and timestamp. Webhook payload fields map only into run parameters validated against `graph.params` (design review finding [webhook and sensor hardening](../reviews/review-2026-10-15.md#webhook-and-sensor-hardening)).

**Target** (FR-6.11, [ADR-0014](../../docs/decisions/0014-target-identity.md)). `graph.target` is optional and names the business target a run works on, so that two correct triggers for one target follow a declared policy and the target's history is readable across runs ([api-and-agent-surface.md](api-and-agent-surface.md) section 2). `key` takes one of two forms: `{ param: package }` binds `run.target_key` when the run is created, from the named run parameter, which must be a string in `graph.params`; `{ from: find_target, output: target, path: task_id }` binds it when the complete source result is successfully accepted and promoted, from the field the path names in its payload, which must resolve to one string. An absent optional parameter leaves the run unbound with no target policy; an invalid present key is validation failure, not absence. The source resolves to one top-level `graph.nodes` ID, not a dynamic body instance. All its outputs, including implicit results, remain staged until successful atomic acceptance of the complete result, its key and source success (ADR-0019). Once bound, the key never changes or clears, including after hold release; same-key rediscovery is accepted and a different valid key rejects the whole production with non-retryable `config_error`, reason `target_identity_conflict`. Missing/malformed key data or incomplete results are invalid output, never a partial binding. The key is opaque: it matches `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`, is written to the run row only and is not a template variable; the parameter or output field it is read from stays under the existing rules. `on_active` says what a new run does while another run of the namespace holds the same key: `allow` (when absent) proceeds, `defer` waits at admission until the holder leaves `running` and all blocking execution/publication recovery is settled; acquired holds survive cancellation during an observation gap ([scheduler.md](scheduler.md) section 6); `defer` cancels nothing and closes nothing. `overlap` is evaluated first, at trigger time and per graph, so a graph that declares `defer` sets `overlap: allow`. A declaration whose parameter, top-level node, output or scalar path does not resolve to a string is `IR020`. Equality is case-sensitive with no normalization. A wrapper/collector source checks its exported complete result, not each body instance; its conflict cannot be ignored by skip_failures or converted into an automatically retried body failure. A selected collection path must resolve to one string; missing/out-of-range values fail rather than selecting another element. A rerun has independent discovery, with reliable selection supplied by the graph. Target identity does not certify business-object correctness or roll back already produced effects; ordinary pre-binding/approval-only target admission remains separate. ADR-0020 governs execution occupancy and bounded acceptance without changing these target boundaries.

## 9. Limits (FR-6.5, FR-6.6, FR-9.4)

**Concurrency is live admission policy** (FR-6.10, [ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md)). `runtime.concurrency.max_running_nodes` counts non-terminal physical attempts and `max_running_runs` distinct runs having any such attempt, including historical/unknown work; no-attempt waits reserve neither. Counts span all versions of a graph. Caps come from its highest-numbered active version, falling back to its newest deployed version if none is active, and apply to older pinned runs for admission only. An absent cap adds no graph restriction; other scopes still apply. Public state names the cap-source version. Execution topology, inputs, target declaration and attempt semantics stay pinned. Lowering caps does not evict work, and another attempt in an already counted run adds no run slot but still obeys attempt caps. Namespace `max_pending_runs` is a separate positive finite outstanding-acceptance bound, default 1000, not an IR run-status counter. The complete state table and same-database contracts are in data-model section 13.

| field | level | kind | enforced by |
|---|---|---|---|
| `timeout` | physical node attempt, subgraph instance, run | hard policy | earliest configured inherited UTC deadline; API/scheduler guard authority and runtime/common executors interrupt/escalate locally through all managed phases without API connectivity; nominal grace/fault model in ADR-0022 |
| `max_events` | agent node | hard | session runner, counting turns, tool calls and tool results in the SDK event stream; class `events_exhausted` |
| `budget_usd` | agent node, subgraph, run | soft | agent node: session runner from streamed usage, also passed to the harness where supported; subgraph and run: the scheduler from the sum of usage records and live counters, admitting no further attempt in the scope while running ones finish ([scheduler.md](scheduler.md) section 4); class `budget_exhausted` |
| `budget_headroom_pct` | `runtime` section | – | tolerated overshoot before termination (NFR-8.2) |
| `stall_after` | agent node; runtime default `15m` | hard | session runner, from silence in the SDK event stream; class `stalled` (DD-15) |

The session runner inside the runtime is the only enforcer of the attempt-level limits `max_events`, `budget_usd` and `stall_after` (design review finding [one enforcement point](../reviews/review-2026-10-15.md#one-enforcement-point)); the worker relays its counters to the control plane and never enforces; the scheduler owns aggregate run/subgraph budget decisions, which no single session can calculate. Timeouts instead propagate the earliest already-known scope deadline into every managed phase (ADR-0022). Run time starts at promotion, subgraph time at first ready activation, node-attempt time at first executing claim before preparation. Existing parent clocks survive retries/clear/adoption. Missing optional limits add no deadline. Interruption and forced-stop escalation have nominal thresholds at `D` and `D + 30s`; grace never restarts on recovery, and actual stopping/remote effects are not a hard-real-time promise. See [scheduler section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022).

## 10. Validation rules (`kora validate`)

Schema validation first, then semantic checks, each with a stable code and a YAML path (FR-0.4): `IR001` acyclic graph (including `depends_on`), `IR002` every `from` resolves, `IR003` output/input type assignable, `IR004` fan-out `over` names a list-typed input and every `collect` names a body output, `IR005` `binds` paths resolve to a `VcsRef` and digest-bearing artifacts, `IR006` `$previous_attempt` only inside a retried subgraph, `IR007` secret aliases resolve, fall inside the namespace's allowed reference prefixes and no value-like strings appear in `env`, `IR008` harness cannot honour a field (warning), `IR009` runtime lacks a requested capability such as `enforces_network_policy` or `supports_resume`, or the node's runtime and VCS declare no common plan executor ([protocols.md](protocols.md) section 4) (warning for optional capabilities; no common plan executor is an error), `IR010` branch targets exist in their graph/body scope, each has the owning branch directly in `depends_on`, no target has multiple distinct branch owners, neither branches nor ordinary direct targets set `attempt_invariant: true`, and `$fail_attempt` appears only inside a subgraph, `IR011` CEL expressions parse and type-check against the declared artifact schemas, `IR012` Jinja2 templates parse in the sandbox, `IR013` `tool_policy` patterns are valid, `IR014` sensor `config` fails the sensor's `config_schema`, `IR015` a node that receives secrets runs on a runtime that declares `enforces_network_policy: false` while the namespace sets `require_network_enforcement`, `IR016` run parameters appear in a `cmd` element or `workspace.rev` of a graph with a webhook trigger, `IR017` `trust_repo_config: true` on a node whose namespace policy does not allow it (design review finding [tool deny coverage](../reviews/review-2026-10-15.md#tool-deny-coverage)), `IR018` an agent node's `harness` names no harness adapter registered with the installation, which in MVP includes every reserved `acp:<agent>` name (an error, not a warning: unlike `IR008` and `IR009`, the node could not run at all; ADR-0004), `IR019` a `tool_policy` pattern from the namespace policy names a call kind the node's harness adapter does not intercept (`HarnessCapabilities.policy_coverage`, [protocols.md](protocols.md) section 12), an error because the platform team's rule would otherwise run unenforced; the same pattern from the node alone is warning `IR008` (ADR-0009), `IR020` `graph.target.key` names a run parameter that is not a string in `graph.params`, or a node, an output or a path that does not resolve to a string, checked against the output's declared schema like `IR011` (FR-6.11, ADR-0014), `IR021` the selected runtime lacks `protected_execution` while namespace policy requires it (ADR-0016); local remains valid for trusted development/tests without that requirement. `IR022` rejects missing/inconsistent type closure, unsupported schema dialect or behavior, invalid schema references and incompatible author-side contract declarations (section 2.1). `IR023` rejects an `independent_of` entry that is not a declared input name of that node.

## 11. Worked examples

- [examples/sql-optimization.ir.yaml](examples/sql-optimization.ir.yaml): the canonical graph from `positioning.md`, as written in the DSL example of [dsl.md](dsl.md) section 1: target → baseline → hypotheses → fan-out of a retried subgraph (implement → dev run → equivalence → review → branch → pack candidate by a command node), the fan-out node collecting the candidates → select best (a command node: gain over the baseline, the 15 % threshold, ties broken by the lowest commit) → review bundle narrative (cheap agent) → VCS approval with kora-attached cost → wait for deploy → timer → metrics check. The two scripts, `tools/gates/pack_candidate.py` and `tools/gates/select_best.py`, live in the graph repository under its protected paths and belong to the end-to-end fixture of the canonical graph ([protocols.md](protocols.md) section 13); `test_candidate_selection_exact_and_reproducible` covers threshold edge values, ties with that tie-break, the empty set, an invalid baseline, a missing metric, that no reference outside the input set can appear in `best`, and that identical inputs give an identical output digest.
- [examples/dependency-upgrade.ir.yaml](examples/dependency-upgrade.ir.yaml): a small cron graph: Codex upgrades one dependency, a command node runs the tests, an approval on the CLI channel, then a sensor waits for the merge.

Both validate against the schema.

Publication-producing nodes follow [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md): a `VcsRef` is derived from trusted prepared/publication results, not an agent-reported branch. Existing candidate commits are preserved; uncommitted changes become one additional prepared commit. Publication credentials are provider configuration, not a required node `secret_env`. Explicitly granting candidate code a code-host credential grants it independent external side effects.
