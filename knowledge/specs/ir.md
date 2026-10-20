# kora MVP design — the YAML intermediate representation (IR)

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: the canonical graph representation that `kora compile` emits, `kora validate` checks and the scheduler consumes (FR-1.2–1.7, FR-2, FR-3.2, FR-6.3–6.6, NFR-6). The machine-readable contract is [ir.schema.json](ir.schema.json), published at a stable `$schema` URL and registered in SchemaStore so editors validate `*.ir.yaml` files; worked examples are in [examples/](examples/). Design decisions taken by the owner on the choices the requirements left open are recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md) and referenced here as **DD-n** ("design decision n").

## 1. Design rules

1. **The IR is data, not code.** The scheduler never imports user Python; everything it needs is materialised in the file, including resolved per-node configuration (FR-3.2) and expressions for branches and predicates (section 7).
2. **Stable string ids and named references**, never positional edges (FR-1.5). Node ids are unique within a graph document and match `^[a-z][a-z0-9_]{0,63}$`.
3. **Three concerns in three sections** (FR-1.3): `graph` (topology and node definitions), `runtime` (graph-level runtime and policy configuration), `secrets` (references only, never values). Compiler provenance (preset and defaults snapshots) lives under `meta`, outside all three.
4. **Lenient at the root, strict inside typed objects** (FR-1.5): unknown root keys are ignored with a warning; unknown keys inside node, retry, limits and config objects are errors. `x-` prefixed keys are allowed on IR record objects, including nested records, for tool metadata. Named maps (such as nodes, type contracts and secret aliases) still validate their entry names and values; embedded JSON Schemas and user payloads retain their own rules.
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
  approval_channels: { vcs: { provider: github, accept_merged_as_approval: false, feedback_from: collaborators_write, request_reviewers: ["dwh-reviewers"] }, cli: {} }   # DD-35; request_reviewers notifies, it does not authorise (ADR-0025)
  concurrency: { max_running_nodes: 20 }
  limits: { timeout: "72h", budget_usd: 200 }      # whole run
  stall_after: "15m"          # runtime-level default for agent-loop silence; nodes override in limits (DD-15)
  provider_retry: { max_wait: "6h" }   # model-provider limits: total waiting allowed before the node fails with provider_limited; namespace default when absent (ADR-0024)
  retention: { transcripts: "30d", artifact_payloads: "30d" }   # graph-level, namespace default applies when absent (DD-4)
  protected_paths: ["Makefile", "tests/**"]     # repository files no agent node may edit; verified before every command gate (DD-34); gate scripts travel as command resources (section 4)
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

Artifact type names are a built-in or namespaced type, recursively wrapped in `List[T]` where needed. The JSON Schema checks the structural spelling; semantic type validation (`IR003`) parses the complete type and checks balanced brackets at every nesting level. The schema alone cannot establish arbitrary bracket balance and imposes no list-depth limit.

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

**Edges** are the union of `inputs[*].from` and `depends_on`. An input names the producer node and one of its declared outputs; the validator checks that the producer's output type is assignable to the input type (the same saved type contract, or a subtype through the graph version's saved parent closure, FR-4.3 and ADR-0018); a subtype keeps the CEL type of every parent property, so it cannot narrow `number` to `integer` (section 7.4). Reserved sources: `$params` (run parameters), `$previous_attempt` (section 5), and `$inputs` inside a subgraph body (the subgraph's declared inputs, by `path`; in a fan-out body the mapped input resolves to the current list element). There is exactly one way to feed a body: the `inputs` of the `subgraph` or `fan_out` node. Inputs declared with `optional: true` may be unresolved when the producer was skipped; others make the node `upstream_failed`/`skipped` according to the trigger rule.

**Input independence** (FR-6.8a, ADR-0007): `independent_of` contains names from this node's `inputs`, not producer IDs. Unknown names fail semantic validation with `IR023`. It preserves only an already successful outcome on the affected input edge; any other affected input or control edge still invalidates the node. Consumers without a successful result are reevaluated. Runtime invalidation resolves declared edges even before input binding, through body input forwarding and exports/collection, together with current invariant-reuse bindings ([data-model section 6](data-model.md#6-clear-downstream-fr-68a)). It never waives a branch/control gate.

**Outputs** declare names resolved exclusively through the compiled `type_contracts` and the deployed closure, not current registry aliases (ADR-0018). Built-in names: `StructuredOutput`, `VcsRef`, `AgentSession`, `CommandResult`, `ReviewBundle`, plus `List[<type>]` for fan-out results and user types registered under `namespace/Name`. A `StructuredOutput` carries its JSON Schema inline under `schema`. **Agent output contract** (DD-36): an agent node declares at most one `StructuredOutput`-derived output and at most one `VcsRef` output besides the implicit `session`; `output_schema` is copied into the artifact's schema; the worker publishes the workspace only when a `VcsRef` output is declared, and a declared `VcsRef` with no diff fails with `artifact_invalid`. The `AgentSession` payload carries harness name and installed version, session id, transcript `storage_ref`, usage per model with completeness, cost estimate with basis and billing class, turn count, duration, `permission_denied` events with whether the harness-side ones are complete, stop reason, and provenance (worker id, image digest). A `CommandResult` payload carries the exit code, the duration, stdout and stderr references with truncated tails, and the shielding of the measurement (DD-34; [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)): `protected_paths`, the resolved list in force for the node, `base_commit`, the commit the digests were compared against, and `protection`, `verified` when every listed path was unchanged against `base_commit` before the command ran and `unprotected` when the node declares no protected paths; a changed protected path fails the attempt with `protected_path_modified` before any `CommandResult` exists. All of them enter the `ReviewBundle` facts.

A **`ReviewBundle`** (DD-33) has two parts: `facts`, written by kora from its own records and read-only to agents (every input artifact of the bundle node with type, digest, producing attempt, worker and image; gate results from `CommandResult` inputs with exit codes, protected paths, base commit and verification result, so that each gate is labelled as verified against its protected paths or unprotected; metrics references; the `VcsRef` with commit; the run's attempt cost), and `narrative`, the only part the agent node writes (`summary`, `rationale`). Approvals bind to the digests of `facts`; `kora web` and the change-request comment render the two parts visibly apart, each gate with its label. There is no node kind for building artifacts (DD-8, ADR-0012): packing a `Candidate` or selecting the best candidate by a fixed formula is a command node over its materialised inputs (section 4), because a copy and a formula need no model and must be reproducible; writing the `ReviewBundle` narrative, or a selection that needs judgement, is an ordinary agent node on a cheap model from its typed inputs. kora itself attaches the aggregated cost of all attempts to the approval request (section 4.2); the agent never computes cost.

**Trigger rules** (FR-1.6 plus DD-1): `all_success` (default), `all_done`, `one_success`, `one_failed`, `none_failed`, `all_skipped`, and `none_failed_min_one_success` (borrowed from Archon: no upstream failed and at least one succeeded, so a node after an all-skipped branch stays skipped). The enum is closed in IR v1.

## 4. Node kinds and their `config`

| kind | purpose | required config | produces |
|---|---|---|---|
| `agent` | one harness session | `harness`, `prompt`, resolved harness config (section 4.1) | declared outputs + `AgentSession` |
| `command` | deterministic gate | `cmd` (argv, no shell), optional `output_file`, `env`, `secret_env`, `network_policy` (enforced by the runtime like an agent node's, ADR-0009), `workspace`, `success_exit_codes` (default `[0]`) | `CommandResult` (exit code, duration, stdout and stderr references, truncated tails, `protected_paths`, `base_commit` and `protection`, section 3) and, with `output_file`, one declared output of type `StructuredOutput` or any custom artifact type whose schema the file must satisfy; stdout and stderr lines stream as session events (DD-36) |
| `human_input` | approval gate | `channel`, `form`, `timeout`, `on_expiry`, `binds` | `StructuredOutput` (form response) |
| `wait_event` | sensor | `event` or `poll`, `interval`, `timeout`, `on_expiry` | `StructuredOutput` (event payload) |
| `timer` | durable delay | `duration` or `at` (absolute time) | none |
| `branch` | deterministic routing | `cases: [{when, to}]`, `else` | none; durable selection gates its direct targets (section 4.5) |
| `fan_out` | map a list over a subgraph | `body` (subgraph id), `over` (name of the list-typed input), `max_parallel`, `skip_failures` | per-item instances; the node's own outputs are `List[<type>]` collected from the body's outputs (`collect`) |
| `subgraph` | graph as a node | `body` (subgraph id); the node's `inputs` feed the body | the body's declared outputs |

**Command inputs.** Before a command starts, the worker writes the payload of each of the node's inputs to `.kora/inputs/<input name>.json` in the workspace, a `List[...]` input as a JSON array of element payloads, so a script reads lists and nested artifacts from files; `cmd` elements may still carry scalar fields through templates (`{{ inputs.target.task_id }}`). `.kora/` is kora's own directory in every workspace, excluded from publication ([protocols.md](protocols.md) section 4).

**Command resources** (issue #45). A command node may declare `resources`: a list of `{name, sha256, size, executable}` and a `resource_set_digest`. `name` is a relative path in Unicode NFC with `/` separators, no empty, `.`, `..` or absolute segments, no control characters (U+0000 to U+001F, U+007F) and no backslash, unique within the node. `sha256` is `sha256:` followed by the lowercase hex SHA-256 of the file's bytes. `resource_set_digest` is `sha256:` followed by the lowercase hex SHA-256 of the **manifest bytes**: the node's entries as a JSON array of objects with exactly the keys `executable` (boolean), `name` (string), `sha256` (string) and `size` (integer), sorted by `name` compared as UTF-8 byte strings, serialised by the JSON Canonicalization Scheme (RFC 8785: keys in code-unit order, no whitespace, minimal string escaping), encoded as UTF-8 with no trailing newline. Other mode bits are not part of the identity. Test vector: a file `select_best.py` holding the 11 bytes `print('a')` plus a newline (`sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb`, not executable) and an empty executable file named `bin/run "ñ".sh` (`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`) give the manifest bytes `[{"executable":true,"name":"bin/run \"ñ\".sh","sha256":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","size":0},{"executable":false,"name":"select_best.py","sha256":"sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb","size":11}]` and `resource_set_digest` `sha256:ac238bb1de5db1d8c8cee691626af721119ffc241431f22603ce77c22af1e305`; the compiler, the API and every runtime must reproduce it. Changed content under an unchanged name changes the file digest, the set digest and the IR digest, so it deploys as a new graph version; no package name or version enters the identity. The content is not in the IR: `kora compile` writes it next to the IR as the resource bundle `build/<graph_id>.resources/<hex>`, one file per distinct digest, and the deploy request carries it ([dsl.md](dsl.md) section 2, [api-and-agent-surface.md](api-and-agent-surface.md) section 2). `cmd` refers to a resource as `{resource_root}/<name>`; the runtime materialises the verified bytes there, outside the candidate root and read-only for candidate code on runtimes declaring `protected_execution` ([protocols.md](protocols.md) section 2). The authoritative bytes are the stored, verified content; a repository file with the same name is never consulted. A resource is executed through an interpreter named in `cmd` and present in the runtime image; read-only resources protect file integrity and do not make a gate an independently trusted evaluator (ADR-0009). The `CommandResult` records the `resource_set_digest` and the per-file digests verified immediately before execution, with the runtime's `resource_protection` (`protected` or `none`); the `ReviewBundle` facts show them next to each gate.

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
  mcp_servers: { warehouse: { type: stdio, command: "warehouse-mcp", args: ["--cluster", "prod"], digest: "sha256:…", secret_env: { YT_TOKEN: YT_TOKEN }, network_policy: { egress: ["warehouse.internal"] } } }   # pinned by package or image digest like skills; a server's secret_env and network_policy are its own and never reach the agent (ADR-0009); agent nodes never get kora's own MCP server (DD-5)
  tool_policy: { allow: ["Read", "Edit", "Bash(etl *)", "Bash(git *)"], deny: ["Bash(rm -rf *)"] }   # the one tool-call policy, evaluated by the policy protocol at every tool call the harness does not reject itself, the harness's output channel excepted (DD-29)
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

`tool_policy` is the input to the **policy protocol** ([protocols.md](protocols.md) section 12, DD-29) and the only tool-permission field in IR 1.0: the DSL's `allowed_tools` and `disallowed_tools` compile into it. The harness's pre-tool-call hook asks the session runner before every tool call the harness does not reject itself; the runner evaluates the namespace's `tool_policy` (set by the platform team, prepended) and then the node's, and a deny from either stops the call and is recorded as a `permission_denied` session event on the `AgentSession`. A call a harness permission rule denies is blocked without reaching the runner and, on an adapter that reports such denials, is recorded from the harness's report with `source: harness`; a call a built-in tool's own input validation rejects never runs and is an ordinary failed tool result, not a denial ([protocols.md](protocols.md) sections 3 and 12). The one exception to the policy is the harness's output channel: on `claude` the runner allows the synthetic `StructuredOutput` tool of a node with `output_schema` without evaluating either policy, under the conditions of [protocols.md](protocols.md) section 3 (ADR-0009, amended 2026-10-19). Patterns use the harness tool-pattern syntax; argv-level matching rejects shell chaining metacharacters and `-c` or `--exec` style indirection inside an allow-listed command. The field is inherited through the preset layers. `Edit` and `Write` on `protected_paths` (DD-34) are denied for every agent node of the graph. MVP ships the `allow_deny` implementation; Cedar or OPA arrive as plugins without an IR change.

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
  approve_when: "response.decision == 'approve'"   # CEL expression that makes the node succeed; false fails it with class approval_rejected, an evaluation error with config_error (section 7.7)
```

kora attaches to every approval request the run's aggregated estimated cost so far (the sum of usage records over the run at request time, [data-model.md](data-model.md) section 5), so the approver sees it next to the form without any node computing it (DD-8). Expiry is a `wait` row like every other timed wait (`timeout` becomes `wake_at`).

For the `vcs` channel (DD-35) the approval counts only when the approver's stable principal is neither a recorded publication principal of the change request nor in the binding's automation-principal set ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)) and, when the namespace sets `approver_allowlist`, is on that list; "merged" is a separate opt-in signal (`accept_merged_as_approval` in the channel configuration, default false). Feedback comments passed to `fresh_with_feedback` carry author and association, filtered to collaborators with write access by default (`feedback_from: collaborators_write`).

A push to the branch of the bound `VcsRef` while the request is `pending` moves the request to `invalidated` and fails the node with class `candidate_moved`, for every channel ([scheduler.md](scheduler.md) section 5; [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md)). The request is never made again on the same bundle: `candidate_moved` is not retryable (section 6) and a manual retry of this node is refused with `API002`; the author retries the node that produced the bound `VcsRef`, with `fresh_with_feedback` to continue from the pushed commit, and the gates, the bundle and a new request follow through the normal downstream path. Before this node's downstream starts, the scheduler checks once more that the approved SHA is still the branch head.

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
  predicate: "has(outputs.verdict) && outputs.verdict.approved == false"   # optional extra condition on artifacts; outputs.* reads need has() (section 7.7)
  strategy: fresh_with_feedback                       # agent nodes only: fresh | resume | fresh_with_feedback
```

Error classes (closed enum, used by `retry_on`, `non_retryable` and the data model): `harness_error`, `command_failed`, `timeout`, `events_exhausted`, `budget_exhausted`, `artifact_invalid`, `attempt_failed`, `approval_rejected`, `approval_expired`, `candidate_moved`, `wait_expired`, `stalled`, `protected_path_modified`, `secret_leak`, `provider_limited`, `integrity_error`, `infrastructure`, `cancelled`, `config_error`. Execution uncertainty is not an error class or an IR retry option: lease expiry preserves the original non-terminal attempt and reservations. ADR-0017 blocks all new runtime admissions in its run while execution/publication recovery is unresolved. Recovery can settle by affirmative adoption of the same live execution or its authoritative immutable prepared/completed checkpoint once every earlier grant/operation is closed; direct replacement still requires runtime termination and every dispatcher/platform publication closure. Manual retry records clear/cancel intent while blocked; a linked rerun is refused with `API002` for any replacement-blocking condition. A separately triggered run follows the ordinary target policy. `infrastructure` and `provider_limited` never consume **agent** attempts: `infrastructure` is retried under the separate `infrastructure_retry` policy at `runtime` level (NFR-2.4), and `provider_limited` under `runtime.provider_retry`, which waits until the provider's reset (or with back-off from 1 to 30 minutes when none is reported) and fails the node only after `max_wait` of total waiting; neither class may appear in a node's `retry_on` (ADR-0024, NFR-2.6). `stalled`, `protected_path_modified` and `secret_leak` are ordinary classes decided by the node's policy. `config_error`, `cancelled`, `candidate_moved` and `integrity_error` are never retryable; `integrity_error` is a command resource whose bytes do not match their pinned digest before execution (Archon's "never retry config errors"; a retried approval node would request again on the bundle built for the old commit, [scheduler.md](scheduler.md) section 5).

`strategy: resume` requires the runtime capability `supports_resume`; when the runtime lacks it, when the session handle is gone or when the installed harness version changed, the attempt falls back to `fresh_with_feedback` and records `strategy_fallback` (FR-6.4). In MVP neither reference runtime declares it (`local` and `docker` release the workspace that holds the harness checkpoint, [protocols.md](protocols.md) section 2), so `kora validate` warns `IR009` for `strategy: resume` on them and every such retry takes the fallback; `fresh_with_feedback` is the strategy that works on every runtime and the documented default where the next attempt needs the previous attempt's work.

## 7. Expressions and templates

Branch conditions and reasons, retry predicates, `approve_when`, event `match` and sensor `until` are **CEL** (Common Expression Language) expressions (DD-3). kora parses and checks every expression with its own parser and schema-driven checker, then evaluates it through the Rust-backed `common-expression-language` binding, which the `kora[cel]` extra installs ([ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md) as amended under issue #126). Other SDKs use any conforming CEL implementation. Only CEL's standard functions and macros are available, narrowed by the rules below. There are no custom functions and no user code. The parser, limits, checker rules, projections, conversions, data bounds and cost model in this section belong to the execution semantics `kora.execution.v1` ([data-model.md](data-model.md) section 11). Every installation applies the same rules, and changing one of them needs a new semantics identity. A new pin of the binding may run under `kora.execution.v1` only if its measured costs stay within the v1 cost weights and its measured crash thresholds stay clear of the v1 limits; otherwise it needs a new semantics identity, so a stored estimate never describes a binding it does not bound. Measurements of the binding cited here are dated evidence from the [CEL binding probe](../research/cel-binding-probe-2026-10-19.md), not guarantees.

### 7.1 Expression kinds and environments

| field | variables | result | evaluated by | an evaluation error (section 7.7) |
|---|---|---|---|---|
| `branch` `cases[].when` | `inputs`, `params` | `bool` | scheduler, branch pass | fails the branch with `config_error`; its direct targets become `upstream_failed` (section 4.5) |
| `branch` `cases[].reason`, `else_reason` (evaluated only on a `$fail_attempt` route) | `inputs`, `params` | `string` | scheduler, branch pass | fails the branch with `config_error`; the iteration does not end as `attempt_failed` |
| `retry.predicate` (node and subgraph) | `outputs`, `inputs`, `params` | `bool` | scheduler, retry handling | no retry; the node keeps its original error class |
| `human_input` `approve_when` | `response`, `inputs`, `params` | `bool` | the shared approval transition (direct API response or channel observation) | the request becomes `failed` and the node fails with `config_error` |
| `wait_event` `event.match` | `payload`, `inputs`, `params` | `bool` | scheduler, wake-ups | the event does not match this wait |
| `wait_event` `poll.until` | `observation`, `inputs`, `params` | `bool` | scheduler, prepared-observation apply | "not yet": the wait keeps polling |
| sensor trigger `poll.until` | `observation` | `bool` | scheduler, start-sensor apply | "not yet": no run starts |

- `inputs.<name>` is the node's declared input, bound from the stored projection of the consumed artifact version (section 7.5). An input declared `optional: true`, and `$previous_attempt`, is a non-required member.
- `outputs.<name>` is an output of the failed attempt, or an export of the failed subgraph attempt, that passed validation and the data bounds. Every member is non-required.
- `params` are the run parameters, typed by `graph.params` and bound from the projection cut at run creation.
- `response` is the approval form response, typed by `form`, projected and bound-checked when the response is received.
- `payload` is the inbound event body and `observation` the sensor result. Neither has a declared schema; both are `map(string, dyn)`.

### 7.2 Parser, limits and canonical rendering

kora's own lexer and recursive-descent parser follow the CEL grammar of cel-spec, without backtracking. Positions are zero-based offsets in Unicode code points into the decoded field value.

**Characters and literals** (`IR011`):

- Identifiers, digits and whitespace are ASCII: an identifier is `[_a-zA-Z][_a-zA-Z0-9]*`, whitespace is space, tab, line feed, form feed and carriage return, and any other character outside a literal or a comment is rejected. kora never accepts a character that CEL's grammar rejects and then rewrites it in the rendering.
- These code points are rejected anywhere in the expression, inside literals and comments too: C0 controls other than tab, line feed, form feed and carriage return; DEL and the C1 controls; U+2028 and U+2029; the format characters U+00AD, U+0600 to U+0605, U+061C, U+06DD, U+070F, U+0890 and U+0891, U+08E2, U+180E, U+200B to U+200F, U+202A to U+202E, U+2060 to U+206F, U+FEFF, U+FFF9 to U+FFFB, U+110BD, U+110CD, U+13430 to U+1343F, U+1BCA0 to U+1BCA3, U+1D173 to U+1D17A, U+E0001 and U+E0020 to U+E007F (bidirectional, zero-width, tag and other format controls, the Unicode 17 general category Cf); and surrogate code points. The set is pinned here, not read from the running interpreter's Unicode database. Variation selectors are combining marks and stay allowed. An author who needs a rejected character in a string writes its `\u` or `\U` escape.
- A comment runs from `//` to the next line feed, as in the cel-spec grammar. A carriage return inside a comment that a line feed does not follow is rejected, because editors show it as a line break; a form feed does not end a comment. A raw carriage return inside a string or bytes literal, triple-quoted and raw ones included, is rejected too, because the binding would read it as a line feed; authors write `\r`.
- Escapes are those of the cel-spec grammar: `\u` and `\U` are valid only in string literals, and an escape denoting a surrogate or a code point beyond U+10FFFF is rejected. The hexadecimal prefix is `0x`.
- An `int` literal beyond the signed 64-bit range, a `uint` literal beyond 64 bits and a `double` literal whose value is not finite are rejected. A single `-` directly before an `int` or `double` literal that no selection or index follows is part of the literal, as is such a `-` after a run of `!`, so `-9223372036854775808` and `!-1` hold literals and `-1.f` is the negation of `1.f`. A run of two or more `-` stays a run of negations, so `--9223372036854775808` is rejected. kora keeps every operator of a run of `!` or `-`, where cel-go and the binding drop an even run, so `!!x` needs a boolean `x`; the binding only ever evaluates kora's rendering, so kora's reading is the one that runs. The binding's unary negation of the smallest integer wraps instead of overflowing, a deviation the evaluator has to account for.

**Constructs** (`IR011`): message construction; optional syntax (`.?`, `[?`, `{?`); leading-dot qualified names, which have no meaning without containers; backtick-quoted field selection; two-variable comprehensions (a third argument of `all` or `exists`; a third argument of `filter` or `exists_one` is a malformed macro); a `has()` argument that is not a field selection; a macro iteration variable that is not a simple identifier; a reserved word as an identifier (reserved words stay valid as field names after a dot); and functions outside the standard function table.

**Standard function table.** The global functions `size`, `int`, `uint`, `double`, `string`, `bytes`, `type`, `dyn`, `duration` and `timestamp` with one argument; the methods `size()`, `contains`, `startsWith`, `endsWith` and `matches` with one argument; and the timestamp and duration accessors `getDate`, `getDayOfMonth`, `getDayOfWeek`, `getDayOfYear`, `getFullYear`, `getHours`, `getMilliseconds`, `getMinutes`, `getMonth` and `getSeconds` without arguments. The table is the cel-spec standard library narrowed to what binding 0.10.0 executes: it lacks `bool()`, the global form `matches(s, p)` and the time-zone forms of the accessors, so they are rejected. Gaps that depend on argument types (`int(timestamp)`, `timestamp(int)`, date accessors on durations) are left to the schema-driven checker. Every entry is verified by evaluation in the binding, and every pin change repeats that verification.

Static limits. An expression or path over any of them is `IR027`, including a `maxLength` violation found by schema validation:

- At most **4,096 code points**, checked before lexing. `ir.schema.json` carries the same `maxLength`.
- Syntax-tree depth at most **32**. Depth is measured on the built tree: a leaf has depth 0, and every edge from a node to a child adds one level, so each binary-operator operand, conditional `?:` condition and branch, selection, index operand and index, call receiver and argument, unary operand, list element, map key and value, and macro range and body adds one level. Each explicit parenthesis adds one level too, and a macro body counts once. It is checked on every descent of the parser, before recursing, and as each node is built, so prefix nesting (`((((`, `!!!!`, `----`) stops with `IR027` before any recursion limit and chains a parser builds in a loop (`a + a + …`, `a.b.b…`) count fully.
- At most **4 comprehension macros** (`all`, `exists`, `exists_one`, `map`, `filter`) per expression, nested at most **2** deep. A macro nests in another when it appears in that macro's body; a macro in another macro's receiver, as in a chain `l.map(…).map(…)`, does not nest.

Lexing, parsing, checking and the cost estimate are linear in the size of the tree. Checking stops at an expression's first error.

The probe measured these thresholds on one macOS machine:

- the binding rejects bracket nesting from 96;
- a chain of about 12,000 selections or additions crashed the process with SIGSEGV on a main thread;
- on a 256 KiB thread stack, a chain of 300 survived and 1,000 crashed.

The limits keep a wide margin below them on the evaluation thread of section 7.6. Every change of the binding pin re-measures them, separately for compile and for evaluation, under the pin rule above.

**Canonical rendering.** From the checked tree kora writes a canonical text: every operator parenthesized, string literals as double-quoted non-raw literals with only the escapes `\\`, `\"`, `\n`, `\r`, `\t` and `\u`/`\U` for other control and non-ASCII characters, bytes literals the same with `\x` for every byte outside printable ASCII, no comments, single spaces. Validation parses the rendering again with kora's parser and requires the same tree, ignoring grouping parentheses, otherwise `IR011`; this uses only kora's parser, so `kora compile` runs it too. Each node puts at most one pair of brackets around its children, except that a unary operator applied to a numeric literal renders the literal in parentheses, `(-(1))`, so that it does not fold into a negative literal, and a negative literal used as the operand of a selection, index or method is parenthesized. Doubles are rendered in their shortest round-trip form and integers in decimal. The rendering is not held to the author-text limits: its length is at most 16 times the source's (a non-ASCII character in a bytes literal renders as up to four `\xHH` escapes), and its re-parse runs under a depth bound of 2 × 32 + 1 levels, checked before recursion, which keeps its bracket nesting below the binding's bracket limit of 96; the boundary tests compile the maximal rendering of each shape in the binding. The binding compiles only this rendering, never the author's text, so it evaluates exactly the tree kora checked. The IR keeps the author's text.

**Where the binding runs.** Server-side validation (`/validate`, `/plan`, deployment, and `kora_compile` through `/validate`) compiles every rendering with the binding, deduplicated by rendering, and compiles each distinct `matches()` pattern once; a rejection is `IR011`, reported without the binding's message. `kora compile` runs on the author's machine with kora's parser and checker only and never imports the binding ([dsl.md](dsl.md) section 2). Deployment stores the rendering, the projection path sets (section 7.5) and the cost estimate with the graph version, and evaluation never recomputes them.

### 7.3 Paths

`inputs[*].path`, `binds.vcs_ref`, `binds.gate_digests`, `workspace.from_ref`, `human_input` `context` and `graph.target.key.path` are member-access chains restricted to field selection and indexing, such as `inputs.bundle.facts.change` or `artifacts['implement.change']`. They contain no operators and no calls. This is the one path notation in the IR.

- **Parsing.** Paths use the lexer and the length and depth limits of section 7.2: `IR011` for syntax, `IR027` for limits. An index is a non-negative decimal integer literal within the signed 64-bit range or a string literal; a negative, hexadecimal or unsigned index, or an index expression, is `IR011`. A relative path starts with a field name that is not a reserved word, so a key that is not an identifier cannot start one. Resolution keeps its existing codes (`IR002`, `IR003`, `IR005`, `IR020`).
- **Walking.** The walker treats a missing key, an index beyond the list, and a field or index on a value of another JSON type as absent. It never applies negative indexing.
- **Resolution.** kora resolves paths with its own walker; a path never reaches the binding.
- **Roots.** `inputs[*].path` and `graph.target.key.path` are relative to their source value. `binds.*`, `workspace.from_ref` and `context` start at `inputs.`.

`timer` takes a `duration` or an absolute `at` time, never an expression.

### 7.4 Schema-driven checker

Types come from the saved type closure (section 2.1). It is resolved lazily and memoized within one validation, so recursive `$ref` structures are checked in bounded time. Each input is typed from the producer's effective saved output contract at its call site.

A schema is mapped after resolving `$ref` and a single-schema `allOf`. Its type then comes from `type`, or, when `type` is absent, from its keywords: `properties` means an object, `items` or `prefixItems` an array, and `enum` or `const` the JSON type of their values. The first matching row wins:

| JSON Schema | CEL type |
|---|---|
| `type: [T, "null"]`, `anyOf`/`oneOf` of `T` and `null`, or an `enum` of one type plus `null` | nullable `T`, following CEL wrapper types |
| object with `properties` | record of the declared fields, with the `required` set; `additionalProperties` types index reads |
| object without `properties`, or with only `patternProperties` | `map(string, V)`; `V` from `additionalProperties` or `patternProperties`, else `dyn` |
| string, or an `enum` or `const` of strings | `string`, with its allowed values |
| integer | `int` |
| number, or an `enum` or `const` of numbers | `double` (`int` under `type: integer`) |
| boolean | `bool` |
| array with `items` | `list(T)`; with `prefixItems` or without `items`, `list(dyn)` |
| a `type` array of several non-null types, any other `anyOf`, `oneOf` or multi-schema `allOf`, an `enum` of mixed types, the boolean schema `true`, a schema with no type, keyword or `$ref` | `dyn` |

`payload` and `observation` are `map(string, dyn)`, and a `dyn` value is checked dynamically:

- dot selection on `payload`, `observation` or another `dyn` value is allowed and needs no guard, and a missing key is the evaluation error `missing_field`; a schema-typed `map(string, V)` is read by index, guarded by `in` like an open object's extra keys;
- a `dyn` operand satisfies the same-type rule for `==`;
- a `dyn` result passes checking and is checked for exact `bool` (or `string`) when evaluated.

These rules give `IR011`, reported at the expression's YAML path and offset:

- **Identifiers and fields.**
  - An unknown identifier is rejected. `inputs` and `outputs` may appear only under field selection.
  - A dot selection of a field outside `properties` is an unknown field, also on an object that allows additional properties. Undeclared keys of an open object are read only by index, `x['key']`.
  - A comprehension variable that shadows an environment variable is rejected.
- **Enum literals.** A string literal compared with an `enum`-typed value must be one of its allowed values.
- **Numbers.**
  - `==` and `!=` require the same type, so `int == double` is rejected, as in the cel-go and cel-cpp checkers.
  - Ordering across `int`, `uint` and `double` is allowed.
  - Arithmetic that mixes numeric types needs `double()` or `int()`.
- **Optional fields.** Reading a non-required field without a guard on the same path is rejected.
  - The guards are:
    - `has(p)` as any conjunct to the left of the read in an `&&` chain;
    - `!has(p)` as any disjunct to the left of the read in an `||` chain;
    - `has(p)` as the condition of `?:` for its true branch.
  - `'k' in x` guards `x['k']` in the same positions.
  - The parent of a `has()` operand must itself be present or guarded, as in `has(a.b) && has(a.b.c) && a.b.c`.
- **Null.** A comparison with `null` yields `bool`. Selection, ordering, arithmetic or a call on a `null` value is an evaluation error.
- **`size()`** on a `string` or a `dyn` value is rejected while the binding counts UTF-8 bytes instead of code points.
- **`matches()`** takes a string-literal pattern only, checked by the parser of section 7.2. kora parses the pattern in a closed subset of the binding's regular-expression syntax:
  - **Supported:** literal characters and escaped ASCII punctuation other than `<` and `>`; the escapes `\n`, `\t`, `\r`, `\f`, `\v`, `\a`, `\xHH` and `\x{H…}`; `.`; the class escapes `\d`, `\D`, `\w`, `\W`, `\s` and `\S`; `\p{…}` and `\P{…}`, or their one-letter forms, for the Unicode classes of the class-size table (`L`, `Lu`, `Ll`, `N`, `Nd`, `P`, `S`, `Z`, `Greek`, `Latin`, `Han`, `Cyrillic`); bracketed classes, negated or not, with ranges and nested bracketed classes; the anchors `^`, `$`, `\b`, `\B`, `\A` and `\z`; capturing, non-capturing and named groups, each name unique within the pattern and starting with a letter or underscore; the flags `i`, `m` and `s`, with at most one `-` that a flag follows; alternation; and the repetitions `*`, `+`, `?`, `{n}`, `{n,}` and `{n,m}`, each optionally lazy.
  - **Rejected** (`IR011`): every other construct, among them verbose mode `(?x)`, the flags `U`, `u` and `R`, look-around, backreferences, `\Q…\E`, `\<` and `\>` (word-boundary assertions in the binding), POSIX classes, class set operations (`&&`, `--`, `~~`), a range that starts or ends at a class escape, and Unicode classes outside the table.
  - **Nesting.** Groups and bracketed classes share one nesting counter of at most 32, checked before the parser recurses.
  - **Size estimate.** Each atom is weighted by its compiled size from class-size tables pinned under `kora.execution.v1`. An `i` flag anywhere in the pattern applies the case-insensitive weights to the whole pattern. A bracketed class is charged the sum of its members' weights, and a negated one at least the weight of `.`; a range that reaches beyond ASCII and a negated Unicode class are also charged at least the weight of `.`. A repetition multiplies by its upper bound, or by its lower bound plus one when it is unbounded (so `+` multiplies by two and `*` by one), and adds a fixed overhead.

  A pattern over the nesting limit or over the cap of its estimate is `IR027`. The tables and the cap are set by the calibration of section 7.6. The server-side compile also compiles the pattern in the binding.
- **Result type.** The result must have the field's type from section 7.1.

A child type must keep the CEL type of every parent property: assignability (`IR003`, section 2.1) rejects a child that narrows `number` to `integer`.

Diagnostics come out in YAML-path order, then offset order, one per expression. A response carries a bounded number of them and states how many it omitted.

### 7.5 Projections and typed conversion

**Projection path sets.** At deployment the checker records, for every output an expression can read, the set of paths it reads, and freezes these path sets in `graph_version.execution_contract`.

- **Notation.** A path set is a sorted list of section 7.3 paths, in which `[*]` stands for every element of a list and `['k']` for a map key.
- **Composition.** Paths compose through every route by which a value reaches an expression:
  - `inputs[*].path`;
  - `$inputs` forwarding into a body;
  - the fan-out element, which becomes `[*]` on the mapped list;
  - `collect` and subgraph exports;
  - `$params`, cut from `run.params`;
  - `$previous_attempt`, including `artifacts['n.o']`, whose paths compose into the earlier iteration's output.
- **Whole subtrees.** A dynamic index, a comprehension range and a whole-value read project the whole subtree at their path. A path read only through `has()`, or a key of a map-typed operand tested only by a literal-key `in`, projects its presence (a `dyn` operand projects its whole subtree): the field or key is kept with an empty value of its type, so the test gives the payload's answer without copying the subtree.

**Cutting and storing.** Every projection is cut once, by whichever step produces its value. For `AgentSession`, whose stored payload is the transcript, the projectable fields are exactly its session metadata: harness name and installed version, session id, usage per model with completeness, cost estimate with basis and billing class, turn count, duration, stop reason and provenance (worker id, image digest). These are kept after the transcript expires ([data-model.md](data-model.md) section 10) and are distinct from the artifact type's `summary`. Reading any other `AgentSession` field is `IR011`. Transcript content is never projected, so CEL needs no `transcripts` capability.

- **Uploaded outputs.** An uploaded output is cut when it is validated at upload, including a staged target-source output (ADR-0019).
- **Scheduler-produced outputs.** Each is cut in the transition that produces it: a `human_input` response, a `wait_event` payload output, a `PreviousAttempt`, a fan-out collection and a subgraph export. A collection or export is re-cut from its children's stored projections under its own path set, so no storage I/O happens inside a scheduler transaction; the re-cut reads at most each child's stored projection (at most 64 KiB each, for at most 1,000 children), stops writing at the first bound it crosses, and one re-cut counts as progress under the allowance.
- **Run parameters.** They are cut at run creation.

kora cuts from the validated value, converts it as described below, checks it against the data bounds of section 7.6 and stores it inline in the metadata database with the artifact version, or with the run for parameters. A projection keeps its artifact's retention class: the artifact-payload period, forever for reference-only types, and for `AgentSession` the lifetime of its session metadata, which outlives the transcript ([data-model.md](data-model.md) section 10). Listings never load it.

**Binding.** Evaluation binds only stored projections.

- **Absent and unavailable.** A path missing from the value is absent in the projection, as it would be in the payload. A projection that is itself unavailable is the error kind `projection_unavailable`, never absent data. A projection is unavailable when retention has deleted it, when it was never cut, or when it was cut under a different path set. It follows its artifact's `payload_state`, except that an `AgentSession` projection, which holds only session metadata, stays available after its transcript expires.
- **Summaries.** The artifact type's `summary` serves API and console views and is never visible to CEL ([protocols.md](protocols.md) section 8).
- **`payload` and `observation`.** They are projected once per event or observation and per frozen path-set digest, by a bounded walker, and the bounds are checked against that path set. A wait's result therefore never depends on which other waits are open. The projection is a disposable cache bounded by entry count, released when the event's batches finish, and recomputed after a restart.

**Conversion.** It follows the producer's saved contract:

- **Integers.** `integer` becomes `int`, and `1.0` becomes `1`.
- **Numbers.** `number` becomes `double`.
- **Values without a schema** (`payload`, `observation`, `dyn` subtrees). Every JSON number becomes `double`, as in CEL's JSON mapping.
- **Strings.** They stay strings, whatever their `format`.
- **Schema defaults** are not filled in.

**Invalid values at entry.** A value outside the signed 64-bit range under `integer`, or data over a section 7.6 bound, is refused where it enters kora:

- an output fails with `artifact_invalid`;
- a direct form response, and run parameters at trigger, webhook start and rerun, are refused with `API003`, and the static `params` of cron, sensor and graph triggers are `IR027` at deployment;
- a polled channel response is not applied, the request stays `pending`, and a diagnostic is recorded ([scheduler.md](scheduler.md) section 16.3).

**The FR-2.5 example.** In the IR it reads `inputs.review.verdict == "approved" && inputs.bench.cpu < inputs.baseline.cpu * 0.85`.

- With `cpu` declared `number`, a stored `100` becomes `100.0`, so both operands of `*` are `double`.
- With `cpu` declared `integer`, the checker rejects `int * double` and the author writes `double(inputs.baseline.cpu) * 0.85`.
- Without conversion, the binding fails with `TypeError` whenever the JSON value happens to be whole (probe).

### 7.6 Data bounds, cost and evaluation

**Data bounds.** They apply after projection, to every variable:

| bound | limit |
|---|---|
| total size of the bound values, and of each stored projection | 64 KiB of canonical JSON |
| list length | 1,000 elements |
| string length | 16 KiB of UTF-8 |
| nesting depth | 32 |
| map size | 1,000 keys |

Where each bound is checked:

- **At the cut.** Every stored projection is checked against every bound where it is cut (section 7.5). For a fan-out `over` list, each element is checked as well as the list. Over a bound:
  - the producer fails with `artifact_invalid`, including a scheduler-produced collection, export, wait payload output or `PreviousAttempt`, whose producing node fails;
  - a form response is checked when it is received, against the paths of `approve_when` and of every consumer of its output: a direct response is refused with `API003` and a polled one stays `pending`;
  - run parameters are refused with `API003` at trigger, webhook start and rerun, and the static `params` of cron, sensor and graph triggers are checked at deployment (`IR027`).
- **At binding.** The total is checked; over it, the evaluation error kind is `bound_exceeded`.
- **`payload` and `observation`**, at projection. Over a bound, the result is an evaluation error with the meaning of section 7.7.

**Cost estimate.** At validation the checker computes each expression's worst-case cost from its tree and the size bounds of its variables; a smaller `maxItems` or `maxLength` in a schema tightens the bounds. The estimate has two dimensions:

- **Steps.** The work charged by the table below.
- **Memory.** The peak size of live intermediate values.

An expression over either dimension of the budget is `IR027`. The charges follow the pinned binding's measured complexity, not CEL's nominal cost:

| charge | proportional to |
|---|---|
| each evaluation | a fixed base, plus the bound size and value count of every top-level variable the expression references; the evaluator binds exactly those (the binding converts all bound data on every call) |
| a node visit | one unit |
| `all`, `exists`, `exists_one` | the range's bound length times the body's charge; nested macros multiply |
| `map`, `filter` | the square of the range's bound length (binding 0.10.0), plus the range length times the body's charge |
| `+` on an operand that may be a string, bytes, list or `dyn` value; list and map literals | the result size and value count, the sums over the operands, which propagate to every consumer |
| result size and element count of `map` and `filter` | the range length times the body's result size; the element count is the range length |
| comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in` | the operand sizes and value counts |
| conversions and string functions | the operand size |
| `matches()` | kora's size estimate of its literal pattern, for compiling on every call, plus that estimate times the subject length for matching; the compiled pattern also counts toward memory |
| a compile on a cache miss | the canonical rendering's length |
| projection work in a scheduler transaction (re-cutting a collection, projecting an event); charged to the allowance only, not to an expression | the bytes read and written |

**Bounds and arithmetic.**

- **Hereditary bounds.** Each variable carries a bound: its total size in bytes of canonical JSON, its value count, its element count, its string length, and a bound for its elements or for each declared field. An element or field without its own bound inherits its parent's size and value count, and the element count and string length of the data bounds. Every bound on a cycle of declared bounds (a strongly connected component of more than one bound, or a bound that contains itself) loses all its declared children, which are then inherited. Converting from the leaves up, a bound whose converted subtree would be deeper than the nesting bound of 32 also loses its declared children, so no converted bound nests deeper than 32. Both rules depend only on the bounds, never on where an expression reaches them. A declared child must be no larger than its parent in bytes and value count and within the data bounds' element count and string length, every size is a non-negative integer, and a bound's fields must not change while it is converted. An estimate that reaches a bound breaking this raises `BoundError`, an error of the environment's builder rather than a diagnostic, because inheriting from such a parent could lower the estimate; bounds the expression never reaches are not examined. The default bound of a variable is the data bounds below, with a value count of at most half the byte bound plus one.
- **Joined element bounds.** The element bound of a list literal, a concatenation or a conditional joins its operands' element bounds, using the inherited bound for an operand that declares none, whatever the static type. A range of type `dyn` iterates over the join of its element bound and a key bound, a map or record over its keys. A join keeps a type only when both sides have the same type object or the same primitive kind; otherwise it is `dyn`, so a join never compares type structures. The schema-driven checker therefore builds one type object per schema node, so that equal types from one closure are identical.
- **Affine results.** A size computed inside a comprehension is affine in the iteration variable's size, so the total over the range is bounded by the range's own total rather than by the element count times the largest element: `l.map(x, x)` is bounded by the size of `l`.
- **Typed charges.** A charge follows the value's type: `+` on two typed numbers is a node visit, and on anything that may be a string, bytes, list or `dyn` value it is a concatenation. Unless both sides are typed strings, bytes or lists, the result of `+` is charged at least the size of a scalar, so a `dyn` operand never makes an addition look cheaper than its typed form.
- **Arithmetic.** The estimate is one post-order pass in exact integer arithmetic. Each bound object of the environment is converted once per estimate, iteratively, so the estimate's work is linear in the expression and in the number of bound objects and declared fields of the environment. Memory is checked before steps, so an expression over both reports the memory dimension. Memory counts the converted bound data once and every value an operation allocates; a reference to a variable or to part of a value allocates nothing.

**Calibration.**

- The budget targets 10 ms and 16 MiB per evaluation on a named reference CI runner. The memory dimension is in bytes.
- Until a deployment first stores an estimate with a graph version, the weights, the steps budget in estimate units and the pattern class tables are provisional values set from the probe measurements, and changing them needs no new semantics identity. The case-insensitive weights of Unicode classes and of ranges beyond ASCII are part of that calibration.
- The first implementation fixes, from registered measurements, the budget in estimate units, the weight of each charge and the reference runner, and a golden corpus pins the estimate. The evaluator does not ship without them.
- The weights belong to `kora.execution.v1`, under the pin rule at the start of section 7.

Two consequences follow:

- With binding 0.10.0 one `map` over a 1,000-element list takes about 14 ms, so such an expression needs a smaller `maxItems`.
- The memory dimension rejects compounding concatenation inside sequential `map` operations. Four of them, each joining eight copies of a 16 KiB element of a list with `maxItems: 1`, denote 64 MiB while fitting every static limit.

**Evaluation.** It runs in the API and scheduler processes, inside the existing short transactions ([scheduler.md](scheduler.md) section 2).

- **The evaluation thread.** Each process has one dedicated long-lived evaluation thread with a stack of at least 1 MiB, shared by every in-process standalone instance the process starts. The process owns it: it is a daemon thread released when the process exits, and stopping one instance rejects only that instance's queued jobs. Startup sets the thread stack size, creates the thread and restores the previous setting, so no other thread is affected.
  - It runs one job at a time.
  - Evaluations requested from inside a transaction take priority over server-side compile jobs. Compile work is submitted one expression at a time, so an evaluation waits for at most one compile, whose length and pattern size the static limits bound.
  - At shutdown, queued jobs are rejected and the job in flight finishes, since it holds the GIL and cannot be interrupted; the per-evaluation budget bounds the wait, and an external supervisor's kill is the hard limit. Stopping an embedded standalone (`kora test`, `run_scenario`, `kora_test`) waits for that job the same way and never exits the host process. A cancelled caller discards its result.
- **The compiled-program cache.** Compiled programs live in a cache bounded by count, with least-recently-used eviction, keyed by deployment digest and the expression's YAML path. A compile on a miss is charged to the allowance.
- **No interruption.** The binding holds the GIL while it parses or evaluates and cannot be interrupted (probe). These bounds, not a timeout, keep evaluation short.

**Evaluation allowance.** The installation limits the sum of charges evaluated in one scheduler transaction; by default the sum may reach the equivalent of 50 ms, and it may never be set below the per-evaluation budget.

- **Scope.** The allowance applies to every scheduler transaction that evaluates CEL: the branch pass, retry handling, wake-ups and prepared-observation apply.
- **Progress.** Each transaction evaluates at least one expression. When the allowance is used up, the pass commits what it has decided, leaves `needs_scheduling` set for the rest and moves on, rotating across runs and namespaces.
- **Events.** One inbound event is matched against open waits in batches, per run and under that run's lock.
- **Order, not results.** The allowance changes when work happens, never how an expression evaluates. Deferred work may cross a wait's expiry, which the existing expiry rules then decide.
- **Configuration.** It is installation configuration rather than execution semantics.
- **API evaluations.** An evaluation in the API (`approve_when`) is not under the allowance and is bounded by the per-evaluation budget alone.

### 7.7 Evaluation errors

**Error kinds.**

- From the binding's documented exception classes:

  | binding exception | error kind |
  |---|---|
  | `KeyError` | `missing_field` |
  | `TypeError` | `no_overload`, which includes selection on `null` |
  | `IndexError` | `index_out_of_range` |
  | `ZeroDivisionError` | `division_by_zero` |
  | `OverflowError` | `overflow` |

- Raised by kora itself:
  - `bound_exceeded`;
  - `number_out_of_range`;
  - `wrong_result_type`: anything that is not exactly `bool`, or `string` for reasons; Python truthiness never applies;
  - `projection_unavailable`.
- `internal_fault`, logged at ERROR, covers everything the checker should already have prevented: a `ValueError` or `RuntimeError` at execution, and the binding's parser panic.

**Catching.** Exceptions are caught narrowly, around the binding call only; kora's own conversion code runs outside that handler.

- The parser panic class is a `BaseException`. It is resolved exactly when the process starts, and a process that cannot resolve it refuses to start.
- `CancelledError`, `KeyboardInterrupt` and `SystemExit` are never caught.

**The error record.** A recorded error has these fields:

- **`kind`**: one of the kinds above.
- **`field`**: the YAML path of the expression.
- **`offset`**: set only when kora itself locates the fault, null for kinds the binding raises.
- **`event_id`**: for a `match` error, the inbound event's identifier.
- **`observed_at`**: for an `until` error, the time of the observation.
- **`at`**: when the error was recorded.

A node that fails because of an evaluation error fails with `config_error`, reason `cel_evaluation_error`, and carries this record in its details.

**Meaning per kind**, beyond section 7.1:

- **`approve_when`** is evaluated only after the response has passed the staleness, commit and digest, and approver checks (section 4.2). A response refused by those checks is never evaluated. On an error:
  - the response is kept with the request, and the request becomes `failed` ([data-model.md](data-model.md) section 4);
  - `approval.responded` records `applied: true` and the error record;
  - the channel is closed with `notify(resolved)` linking the canonical status, which shows `failed`;
  - the node fails with `config_error`.

  A `failed` request never counts as approval, and a later response is recorded with `applied: false`. A direct API response returns HTTP 200 with the request's status.
- **`retry.predicate`** is evaluated only when the class is retryable, listed in `retry_on` and attempts remain. It is never evaluated for `infrastructure` or `provider_limited`, which do not consume attempts. Every `outputs.*` read needs a `has()` guard (section 7.4). An error is recorded on the node's failure event.
- **`match`.** Each event and wait pair is evaluated at most once, whether the result is false or an error. The evaluating transaction inserts an `event_evaluation` row for the pair ([data-model.md](data-model.md) section 2), together with any consumption. An event is eligible for a wait under the existing matching rules, and an open wait with an eligible event it has not evaluated keeps its run's `needs_scheduling` set. A `match` error is written in that pass-6 transaction, after the exact open wait is verified under the run lock.
- **`until`.** An error counts as a successful observation for dispatch: the token is consumed, the next poll keeps the normal interval and the integration is not degraded ([scheduler.md](scheduler.md) section 16). A start binding consumes no occurrence or cursor and writes no `trigger.*` event. An `until` error is written only from the current claim.
- **Recording external errors.** The last `match` or `until` error is stored on the wait or the start binding and is visible in the API and the console.
- **Unavailable inputs.** `projection_unavailable` fails the owning node with `config_error` for every kind, waits included, because an expired or missing projection never becomes available again. The one exception is `retry.predicate`, where it means no retry and the node keeps its original class, like any predicate error.

**Safe content.** Error records stored on a node failure, a wait, a start binding, an approval request or an event, and every error the API returns, carry only the fields above. They never carry expression text, bound values, binding messages, chained exceptions or panic payloads. Diagnostics follow the same rule ([observability.md](observability.md) section 5), including at DEBUG. The evaluating component emits them, the scheduler or the API, and deduplicates them on the wait, binding or node, the field and the kind.

### 7.8 Conformance

The cel-spec conformance suite runs in CI against the binding, through a hand-written reader of the textproto subset its selected files use, with no protobuf dependency.

- **Vendoring.** The files are vendored under `tests/` at a pinned cel-spec commit, with the upstream licence. Their source URL, full commit SHA and digest are recorded in `knowledge/research/` when the suite is added.
- **Selection.** Explicit include and exclude lists name the files. Excluded are `dynamic` and `namespace` (message types and containers), `macros2` (two-variable comprehensions), `proto2`, `proto3`, `wrappers` and `enums` (message types), `optionals`, `unknowns`, `type_deduction` and every extension file. Within the included files, explicit entries name each case, or a whole section, that kora rejects, with the feature it needs and the diagnostic reason kora gives: message types and qualified type names, backtick-quoted fields, unescaped control characters in literals, the narrowed standard functions and functions outside the standard library.
- **Integrity.** The files are byte-identical to the upstream commit, and a test checks them and the licence against a manifest of SHA-256 digests.
- **Known deviations.** The known deviations so far are string `size()` counting UTF-8 bytes and the unsupported optional syntax. Each is asserted as the binding's exact current outcome, so the test fails when the deviation disappears; a deviation is never skipped.
- **Reader and parser.** The reader has two layers: a closed generic textproto syntax that parses whole files, and a typed case model that fails on any field outside the subset the selected files use. Every included case also runs through kora's parser and canonical rendering: a case without an entry must be accepted, a listed case must give exactly its listed reason, and every entry must match a case.

### 7.9 Future contracts

The parser, checker, cost estimate and textproto reader are unit-tested, as AGENTS.md allows for parsers, checkers and arithmetic. Every test that touches the metadata database runs on PostgreSQL and SQLite. All are deterministic; anything that measures time or memory is a measurement below, not a test.

**Status.** The parser, limits, rendering, paths, pattern subset, cost estimate and conformance reader landed under [issue #161](https://github.com/realkarych/kora/issues/161) in `kora.cel` and `tests/cel/`. These tests are active in full: `test_cel_rejects_expression_over_depth_limit` (its 4,096-code-point leg is `test_cel_prefix_floods_stop_at_the_depth_limit`), `test_cel_rejects_comprehension_over_limits`, `test_cel_paths_obey_limits_and_never_reach_binding` (its binding leg is `test_cel_paths_never_load_the_binding`, in a fresh process) and the rows added below for `kora.cel` other than the two binding rows. These are active in part:

- `test_cel_canonical_rendering_matches_author_text`: the re-parse leg over the corpus, the included conformance cases and expressions generated from a fixed seed; the leg that compares results in the binding waits for the binding.
- `test_cel_rejects_expression_over_length_limit`: the expression leg; the schema-validation leg waits for the `ir` validator.
- `test_cel_rejects_non_literal_regex_pattern`: the literal rule; the backreference leg at server-side validation waits for the API.
- `test_cel_regex_compile_cost_bounded`: the nesting and cap legs in `kora.cel`; the `kora compile` and `/validate` legs and compiling each distinct pattern once wait for the DSL and the API.
- `test_cel_cost_estimate_rejects_compounding_concatenation`: with the bounds given directly; the `maxItems` leg waits for the schema-driven checker.

The binding legs, the evaluator tests, the golden corpus and the conformance suite through the binding wait for [issue #162](https://github.com/realkarych/kora/issues/162); the checker, projection, API, scheduler and DSL legs stay `pending implementation` with the work items that own them in the implementation plan.

| Test | Required result |
|---|---|
| `test_cel_rejects_unknown_field` | `response.decison`, a dot selection of an undeclared field on an open object and a dot selection on a schema-typed map are `IR011` at the right offset |
| `test_cel_rejects_non_boolean_approve_when` | `approve_when: "response.decision"` is `IR011` |
| `test_cel_converts_json_numbers_to_schema_types` | stored `100` under `number` binds as `double` and the FR-2.5 example evaluates; under `integer` the checker rejects `int * double`; schema-less numbers bind as `double` |
| `test_cel_rejects_expression_over_depth_limit` | 33 levels of each node kind (`+`, `&&` and selection chains, `?:`, `!`, `-`, parentheses, call arguments, list literals) are `IR027`. 4,096 code points of prefix or conditional nesting are `IR027` without a recursion error |
| `test_cel_rejects_expression_over_length_limit` | 4,097 code points are `IR027`, also when found by schema validation |
| `test_cel_rejects_comprehension_over_limits` | a fifth macro, and a macro in the body of a macro in the body of a macro, are `IR027`; four chained macros are accepted |
| `test_cel_limits_at_boundary_compile_in_binding` | expressions exactly at the length, depth and comprehension limits pass the checker and compile in the binding |
| `test_cel_cost_estimate_rejects_compounding_concatenation` | four sequential `map` operations, each joining eight copies of a 16 KiB element of a list with `maxItems: 1`, are `IR027` because the memory dimension is exceeded, not for a static limit |
| `test_cel_cost_estimate_golden_corpus` | the estimate of every corpus expression is unchanged |
| `test_cel_regex_compile_cost_bounded` | literal patterns over the size-estimate cap or the group-nesting limit, such as `a{1000}{1000}`, `\w{500}`, `\p{L}{500}` and case-insensitive repetitions, together with `\W{500}`, `\P{L}{500}` and `[\p{L}\d]{500}`, are `IR027` in `kora compile` and at `/validate` alike; a pattern nested deeper than 32 groups stops before recursion; a request compiles each distinct pattern once |
| `test_cel_rejects_non_literal_regex_pattern` | `x.matches(y)` with a non-literal `y` is `IR011`; a literal with a backreference is `IR011` at server-side validation |
| `test_cel_binding_compiles_canonical_rendering` | the binding receives only kora's rendering; a lexically ambiguous author text cannot reach it |
| `test_cel_canonical_rendering_matches_author_text` | every rendering parses back to the same tree, ignoring grouping; over a corpus and expressions generated from a fixed seed, author text and rendering give equal results in the binding |
| `test_cel_paths_obey_limits_and_never_reach_binding` | an over-long or over-deep path is `IR027`; resolution never calls the binding |
| `test_deploy_rejects_hand_written_ir_over_cel_limit` | `/validate`, `/plan` and deployment reject a hand-written IR over each limit and over the budget |
| `test_cel_validation_work_bounded` | a 4 MB request of maximal expressions compiles each distinct rendering once and returns a bounded diagnostic list with its omitted count |
| `test_cel_rejects_unknown_enum_literal` | `response.decision == 'aprove'` is `IR011`, also for an `enum` without `type` |
| `test_cel_rejects_mixed_int_double_equality` | `inputs.same.exit_code == 0.0` is `IR011`; `inputs.metrics.cpu_seconds < 100` is accepted |
| `test_cel_rejects_unguarded_optional_field` | an unguarded optional read, and an unguarded `outputs.*` read in a predicate, are `IR011`; each guard form is accepted, including inside conjunction and disjunction chains and nested `has()` chains |
| `test_cel_dyn_selection_needs_no_guard` | `observation.body.ready == true` and `payload.commit == inputs.bundle.facts.change.commit` pass the checker; a missing key at run time is `missing_field` |
| `test_cel_rejects_string_size` | `size()` on a string or a `dyn` value is `IR011`; on lists, maps and bytes it is accepted |
| `test_cel_recursive_closure_checked_in_bounded_time` | recursive and doubly referenced `$ref` closures are checked in a number of checker steps linear in their size, counted deterministically |
| `test_cel_child_type_keeps_parent_cel_types` | a child type narrowing `number` to `integer` is `IR003` |
| `test_cel_examples_pass_checker` | every expression in the example IRs and in dsl.md section 1 passes the checker against published built-in schemas |
| `test_cel_parser_agrees_with_binding_compile` | every corpus expression, and every expression generated from a fixed seed, that kora accepts compiles in the binding |
| `test_cel_server_validation_compiles_with_binding` | an expression the binding rejects is `IR011` at server-side validation without the binding's message, never a server error |
| `test_kora_compile_and_validate_report_same_location` | for faults kora's own parser, checker and estimate find, `kora compile` and `/validate` report the same code, YAML path and offset, with a character outside the Basic Multilingual Plane before the fault |
| `test_kora_compile_does_not_import_cel_binding` | a fresh `kora compile` process never loads the binding |
| `test_cel_projection_matches_full_binding` | evaluating over the stored projection gives the result of evaluating over the full value; a read of an `AgentSession` field outside its summary fields is `IR011` |
| `test_cel_projection_absent_optional_is_absent` | an optional field absent from an available projection is false to `has()`; an expired, never-cut or differently cut projection is `config_error`, and a manual retry needing it is refused with `API002` |
| `test_cel_projection_composes_through_collect_and_exports` | projections through `inputs[*].path`, the fan-out element, `collect`, exports, `$inputs`, `$params` and `PreviousAttempt.artifacts` carry every read path |
| `test_cel_scheduler_produced_outputs_are_projected` | a form response, a wait payload output, a `PreviousAttempt`, a collection and an export are cut in their producing transition; over a bound a wait payload output, `PreviousAttempt`, collection or export fails its producing node with `artifact_invalid`, while a form response is refused at receipt |
| `test_cel_projection_number_kinds_round_trip` | stored `int` and `double` values keep their kind on both databases |
| `test_cel_oversized_projection_fails_producer_with_artifact_invalid` | an over-bound projection fails its producer, including a staged target source, atomically |
| `test_cel_rejects_over_bound_data_at_entry` | an `integer` beyond 64 bits, or data over any bound, fails an output, is `API003` for a direct response and for parameters at trigger, webhook start and rerun, is `IR027` at deployment for static trigger `params`, and leaves a polled response `pending` |
| `test_cel_external_projection_over_bound_is_no_match` | an over-bound `payload` is no match and an over-bound `observation` is "not yet"; with two waits of different graph versions, one wait's wider path set does not change the other's result |
| `test_cel_retry_predicate_ignores_invalid_outputs` | an output that failed validation is absent to `has()` |
| `test_cel_branch_error_fails_with_config_error` | a `when` or `reason` error fails the branch with `config_error`, reason `cel_evaluation_error`, and direct targets become `upstream_failed` |
| `test_cel_approve_when_error_marks_request_failed` | on the API and the channel path the request becomes `failed`, the response is kept, the channel closes and the node fails with `config_error`. The direct response is HTTP 200 with status `failed`, `?status=failed` lists the request, and a later response is `applied: false` |
| `test_cel_approve_when_not_evaluated_without_authority` | responses refused for `API005`, `API006`, approver rules or a stale token are never evaluated; the request stays `pending` and the node is not failed, on both paths |
| `test_cel_retry_predicate_error_keeps_original_class` | a predicate error, including `projection_unavailable`, prevents the retry and keeps the attempt's class; `infrastructure` and `provider_limited` never evaluate the predicate |
| `test_cel_match_evaluated_once_per_wait` | each eligible event and wait pair is evaluated exactly once, and an error is no match and is visible. This holds across ticks and a restart, and when an event with a lower identifier commits late (a PostgreSQL barrier, and the serialized SQLite equivalent) |
| `test_cel_until_error_keeps_waiting_and_is_visible` | an `until` error keeps the wait open and the start binding idle, and records the error from the current claim only |
| `test_cel_until_error_does_not_degrade_integration` | repeated `until` errors leave the integration's failure count and interval unchanged |
| `test_cel_dyn_result_requires_exact_bool` | `observation.body.ready` returning `"false"` is `wrong_result_type`, never true |
| `test_cel_evaluation_allowance_yields_between_batches` | at the allowance, the pass commits, leaves `needs_scheduling` set and serves another run of another namespace; each transaction evaluates at least one expression; the rest continues after a restart |
| `test_cel_binding_exceptions_map_to_error_kinds` | each documented binding exception maps to its kind, with a null `offset` |
| `test_cel_panic_maps_and_preserves_cancellation` | the parser panic becomes `internal_fault`; cancellation during evaluation still propagates |
| `test_cel_persisted_errors_exclude_values` | no stored, returned or logged error carries expression text, values or binding messages. This includes binding messages that contain payload keys, DEBUG output and exception chains, in every sink: wait, binding, approval, event, API, CLI, MCP and logs |
| `test_cel_evaluation_error_reads_parity` | the error record appears in node, trigger-binding and approval reads, the Python client, CLI `--json`, MCP output and the TypeScript client |
| `test_cel_evaluating_entry_points_refuse_without_binding` | the API, scheduler and standalone refuse to start without `kora[cel]`, and `kora test` and `kora_test` return a structured error naming the extra, through an injected binding loader; `kora doctor` reports the missing extra |
| `test_cel_evaluating_entry_points_refuse_mismatched_binding_version` | a binding other than the pinned version, or a `cel` module from another distribution, prevents start |
| `test_cel_conformance_suite` | the vendored suite passes, with every known deviation asserted exactly |
| `test_cel_depth_rule_matches_conformance_boundaries` | the cel-spec `nest` and `repeat` cases at the limit are accepted, and mixed parenthesis and operator nesting counts both |
| `test_cel_lexer_rejects_what_cel_rejects` | non-ASCII digits, letters and spaces outside literals, the rejected control and format code points anywhere, raw carriage returns in comments and literals, surrogates, out-of-range and malformed escapes, `\u` and `\U` in bytes, the `0X` prefix, digit separators, out-of-range literals and unterminated literals are `IR011` with their reason |
| `test_cel_rejects_unsupported_constructs` | message construction, optional syntax, leading-dot names, backtick fields, two-variable comprehensions, reserved identifiers and malformed syntax (unexpected input and unexpected end) are `IR011` with their reason |
| `test_cel_rejects_unknown_functions_and_malformed_macros` | functions outside the table and malformed `has()` and comprehension calls are `IR011` |
| `test_cel_rejects_functions_the_binding_lacks` | each narrowed standard function is `IR011` |
| `test_cel_accepts_every_table_entry` | every entry of the standard function table is accepted in its form and arity |
| `test_cel_accepts_every_macro_form` | every macro is accepted with two arguments and `map` also with three; `all` and `exists` with three are two-variable comprehensions, and `exists_one` and `filter` with three are malformed macros |
| `test_cel_prefix_floods_stop_at_the_depth_limit` | 4,096 code points of prefix or conditional nesting are `IR027` early, without a recursion error |
| `test_cel_reports_code_point_offsets` | offsets count code points, also after a character outside the Basic Multilingual Plane |
| `test_cel_work_is_linear_in_steps` | lexing, parsing, the pattern parser and the estimate take at most a fixed number of counted steps per code point on hostile and maximal input up to the length limit |
| `test_cel_rendering_reparse_is_bounded` | the rendering of every nesting shape at depth 32 re-parses under the bound of 2 × 32 + 1 levels |
| `test_cel_rendering_beyond_the_reparse_bound_is_refused` | a rendering deeper than 2 × 32 + 1 levels is `IR027` |
| `test_cel_rendering_expansion_is_at_most_sixteen` | a maximal bytes literal of non-ASCII characters renders within 16 times its length |
| `test_cel_paths_walker_follows_cel_indexing` | the walker resolves fields and non-negative positions and treats missing keys, positions beyond the list and type mismatches as absent |
| `test_cel_paths_never_load_the_binding` | parsing and resolving paths in a fresh process never imports the binding |
| `test_cel_regex_rejects_constructs_outside_subset` | every construct outside the supported pattern subset is `IR011` |
| `test_cel_regex_accepts_the_supported_subset` | every supported construct is accepted |
| `test_cel_regex_estimate_floors_wide_classes` | wide ranges and negated Unicode classes weigh at least `.`, and `+` weighs as `{1,}` |
| `test_cel_regex_subset_agrees_with_binding` | every pattern kora accepts compiles in the binding, and `''.matches(p)` evaluates over the pattern corpus |
| `test_cel_function_table_executes_in_binding` | every entry of the standard function table evaluates in the binding |
| `test_cel_cost_estimate_bounds_comprehension_results` | the result of `map` and `filter` is bounded by its range's total size |
| `test_cel_cost_estimate_closes_every_comprehension_scope` | a value carrying an iteration variable out of its comprehension leaves no unbounded term, and generated expressions never fail the estimate with an exception |
| `test_cel_cost_estimate_keeps_element_bounds_through_literals` | ranges built by `dyn()`, concatenation and conditionals over a large list keep its element bounds, so a nested quadratic macro over them is `IR027` |
| `test_cel_estimate_is_linear_in_the_environment` | the estimate's counted steps grow by at least one per declared field and linearly with the number of declared fields, and stay small for shared bounds nested 32 deep |
| `test_cel_cost_estimate_charges_follow_the_table` | with one weight set at a time, each weight contributes exactly its formula: the evaluation base, conversion per byte and per value, node visits, iterations, the `map` and `filter` square, concatenation and collection literals per byte and per value, comparisons and `in` per byte and per value, conversions and string functions, pattern compile and match, the compile on a miss, and memory including a compiled pattern; only top-level variables are charged for conversion |
| `test_cel_cost_estimate_rejects_quadratic_macros_over_unbounded_lists` | a `map` over a `dyn` list at the default bounds is `IR027` for steps, and over a small typed list it is accepted |
| `test_cel_cost_estimate_survives_deep_shared_and_cyclic_environments` | an environment bound nested 3,000 deep, a bound that contains itself, distinct shared record types nested 32 deep and cyclic record types return an accepted result, and 16 chained shared bounds each 31 deep return a result rather than an exception |
| `test_cel_cost_estimate_does_not_depend_on_operand_order` | swapping the operands of `&&` around a variable whose declared bounds nest deeper than 32 leaves the step estimate unchanged |
| `test_cel_cost_estimate_cuts_whole_cycles_regardless_of_order` | two bounds that contain each other give the same estimate for an iteration over each, whichever is reached first |
| `test_cel_cost_estimate_refuses_inconsistent_bounds` | a child bound larger than its parent, a child over the data bounds, a negative size and a size that is not an integer raise `BoundError` |
| `test_cel_cost_estimate_never_charges_dyn_additions_below_typed_ones` | a list of `dyn` additions is charged at least the memory of the same list of typed additions |
| `test_cel_accepts_the_standard_functions_and_macros` | representative expressions over the table and the macros are accepted |
| `test_cel_lexer_accepts_cel_literals` | literals, escapes, comments, whitespace including a bare carriage return, folded negative literals and runs of unary operators render as stated |
| `test_cel_paths_accept_selection_and_literal_indexes` | paths of fields, string keys and positions parse, up to depth 32 (a root and 32 selections) |
| `test_cel_regex_rejects_invalid_patterns` | malformed patterns, dangling flag dashes, misnamed or repeated group names and ranges at class escapes are `IR011` |
| `test_cel_textproto_scalars_keep_their_kind` | the generic reader keeps numbers, words and strings apart |
| `test_cel_diagnostics_exclude_author_text` | no diagnostic message, field or exception carries an identifier, literal, pattern or invalid token from the expression |
| `test_cel_textproto_reader` | the reader parses the syntax the vendored files use |
| `test_cel_textproto_reader_refuses_unknown_input` | unknown fields, malformed syntax, integers outside their type's range or grammar, invalid UTF-8, untyped descriptions, unnamed objects and repeated bindings are refused |
| `test_cel_conformance_cases_decode_their_values` | vendored values, bindings and declarations decode to their typed form |
| `test_cel_conformance_data_matches_manifest` | every vendored file and the licence match their recorded SHA-256 |
| `test_cel_conformance_cases_parse` | every included case parses and renders with kora, or gives exactly the reason of its listed rejection; each listed rejection matches a case, each rejection's reason belongs to its feature, a control-character rejection points at a rejected character, a quoted-field rejection holds a backtick, and a rejection for a narrowed or undeclared function names a function inside or outside the cel-spec standard library accordingly |

Measurements, `pending implementation`, register with the evaluator under [issue #162](https://github.com/realkarych/kora/issues/162); the parser's linearity is a counted-steps test above. Peak native memory is measured as RSS per fresh process, because Python heap tracing misses it; accumulation is measured in one long-lived process.

- **Validation:**
  - parse, check and estimate time on hostile input, including class-heavy regular-expression literals;
  - validation of a 1,000-node IR;
  - a maximal 4 MB `/validate` request;
  - compile time and GIL hold of maximal canonical renderings.
- **Cost model:**
  - for each charge at the budget, the measured cost on the reference runner stays within the estimate;
  - the worst case at the budget, including conversion of 64 KiB bindings: time, peak RSS and GIL hold;
  - on every pin change, the gate that keeps the pin under `kora.execution.v1`.
- **Allowance and fan-in:**
  - transaction duration at the allowance with cheap expressions over 64 KiB bindings;
  - one event against 1, 100 and 1,000 waits;
  - projecting a 4 MB event;
  - a 1,000-body fan-out with branches, and collecting 1,000 child projections;
  - NFR-3.1 latency of an unrelated run while evaluations are at the allowance;
  - approval and start latency under concurrent maximal `/validate` requests;
  - event-loop lag in standalone.
- **Crash thresholds:** per node kind, for compile and for evaluation, on Linux x86_64, Linux aarch64 and macOS, on the 1 MiB evaluation thread.
- **Lifetime:**
  - repeated compile and evaluate cycles covering error paths, cancellation and shutdown with queued jobs;
  - repeated in-process standalone start and stop cycles, as `kora test` runs them: threads, caches and RSS;
  - growth of `event_evaluation` rows (open waits times eligible events), the duration of closing and cancelling transactions with maximal rows, their batched background release, and the NFR-3.1 latency of unrelated runs meanwhile, on both databases;
  - a collection re-cut over 1,000 maximal child projections with narrow collection paths;
  - `kora_test` cancellation with a queued and an in-flight job, and host exit;
  - the compiled-program cache with more versions than its bound;
  - metadata growth and listing latency with maximal inline projections on both databases.

### 7.10 Prompt templates

Prompt templates (`prompt`, `system_prompt_append`, `cmd` elements) are **Jinja2 in a sandboxed environment** (DD-11): the full template language, the variable names of section 7.1 (the CEL projections of section 7.5 do not apply to templates), no filesystem loaders, no imports or includes, no access to Python attributes beyond the bound data. Templates are rendered once when the nonsecret execution request is sealed before preparation/execution effects; the attempt owns the saved rendered content and availability state. Recovery reads that content and never re-renders over current data (ADR-0018). Run parameters that arrive from a webhook may be used in prompts but never inside `cmd` elements or `workspace.rev` (`IR016`).

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

Each trigger variant requires its operational fields: cron `expr`; webhook `name` and `auth`; sensor `poll` and `interval`; graph `graph` and `on`. Manual triggers require only `kind`. Fields belonging to a different variant are errors. `params` and `x-` metadata are common fields; `timezone` is cron-only and `params_from` is webhook-only. Semantic validation still checks cron syntax, timezone names, sensor-specific configuration and secret alias resolution.

Every accepted start produces a pinned pending run or a durable no-run overlap-skip outcome, with an idempotency key (section 13 of [data-model.md](data-model.md), ADR-0020). There is no acknowledged unconverted trigger queue. Matching accepted replay precedes current graph/schema/target validation and quotas; full outstanding capacity is a structured retryable refusal, not an accepted delivery. Overlap handling is `graph.overlap`: `allow`, `skip` or `queue` (DD-12). When the field is absent, runs fired by a webhook trigger use `queue` and runs from every other trigger kind use `allow` (FR-6.2, ADR-0006). Skip considers accepted pending and running graph runs; queue promotion selects the oldest pending run under the graph lock only when no graph run is running. Save resolved overlap on each accepted run. Explicit allow and same-run retry retain their semantics. Cron fires forward only; there is no catch-up of missed fire times in v1 (DD-37). Webhook authentication is either an HMAC body signature in the Standard Webhooks shape or a bearer token with a timestamp and nonce inside a replay window, chosen per trigger; the secret is a reference into `secrets.aliases` (DD-17); the delivery's idempotency key is derived from signature and timestamp. Webhook payload fields map only into run parameters validated against `graph.params` (current contract [protocols](protocols.md)).

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

The session runner inside the runtime is the only enforcer of the attempt-level limits `max_events`, `budget_usd` and `stall_after` (current contract [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md)); the worker relays its counters to the control plane and never enforces; the scheduler owns aggregate run/subgraph budget decisions, which no single session can calculate. Timeouts instead propagate the earliest already-known scope deadline into every managed phase (ADR-0022). Run time starts at promotion, subgraph time at first ready activation, node-attempt time at first executing claim before preparation. Existing parent clocks survive retries/clear/adoption. Missing optional limits add no deadline. Interruption and forced-stop escalation have nominal thresholds at `D` and `D + 30s`; grace never restarts on recovery, and actual stopping/remote effects are not a hard-real-time promise. See [scheduler section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022).

## 10. Validation rules (`kora validate`)

Schema validation first, then semantic checks, each with a stable code and a YAML path (FR-0.4): `IR001` acyclic graph (including `depends_on`), `IR002` every `from` resolves, `IR003` output/input type assignable, `IR004` fan-out `over` names a list-typed input and every `collect` names a body output, `IR005` `binds` paths resolve to a `VcsRef` and digest-bearing artifacts, `IR006` `$previous_attempt` only inside a retried subgraph, `IR007` secret aliases resolve, fall inside the namespace's allowed reference prefixes and no value-like strings appear in `env`, `IR008` harness cannot honour a field (warning), `IR009` runtime lacks a requested capability such as `enforces_network_policy` or `supports_resume`, or the node's runtime and VCS declare no common plan executor ([protocols.md](protocols.md) section 4) (warning for optional capabilities; no common plan executor is an error), `IR010` branch targets exist in their graph/body scope, each has the owning branch directly in `depends_on`, no target has multiple distinct branch owners, neither branches nor ordinary direct targets set `attempt_invariant: true`, and `$fail_attempt` appears only inside a subgraph, `IR011` CEL expressions parse and type-check against the declared schemas, and paths parse: unsupported syntax or functions, unknown identifiers and fields, shadowing comprehension variables, enum literals outside the allowed values, operand overloads, unguarded optional reads, `size()` on strings or `dyn` values, non-literal or uncompilable `matches()` patterns, wrong result types, a canonical rendering that does not parse back to the same tree and expressions the binding cannot compile at server-side validation (section 7); path resolution keeps its own codes, `IR012` Jinja2 templates parse in the sandbox, `IR013` `tool_policy` patterns are valid, in the node's and in the namespace's policy, and none has the exact tool name `StructuredOutput`, the harness output channel (a wildcard pattern stays valid and never applies to it), `IR014` sensor `config` fails the sensor's `config_schema`, `IR015` an agent node with `secret_env` lacks a non-empty `network_policy` or runs on a runtime declaring `enforces_network_policy: false`, an MCP server with `secret_env` lacks a non-empty own `network_policy` or runs on a runtime declaring `isolates_tool_secrets: false` or `enforces_network_policy: false`, unless the namespace sets `allow_unenforced_agent_secrets`; a command node with secrets on a non-enforcing runtime while the namespace sets `require_network_enforcement` (ADR-0009 as amended 2026-10-18), `IR016` run parameters appear in a `cmd` element or `workspace.rev` of a graph with a webhook trigger, `IR017` `trust_repo_config: true` on a node whose namespace policy does not allow it (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)), `IR018` an agent node's `harness` names no harness adapter registered with the installation, which in MVP includes every reserved `acp:<agent>` name (an error, not a warning: unlike `IR008` and `IR009`, the node could not run at all; ADR-0004), `IR019` a `tool_policy` pattern from the namespace policy names a call kind the node's harness adapter does not intercept (`HarnessCapabilities.policy_coverage`, [protocols.md](protocols.md) section 12), an error because the platform team's rule would otherwise run unenforced; the same pattern from the node alone is warning `IR008` (ADR-0009), `IR020` `graph.target.key` names a run parameter that is not a string in `graph.params`, or a node, an output or a path that does not resolve to a string, checked against the output's declared schema like `IR011` (FR-6.11, ADR-0014), `IR021` the selected runtime lacks `protected_execution` while namespace policy requires it (ADR-0016); local remains valid for trusted development/tests without that requirement. `IR022` rejects missing/inconsistent type closure, unsupported schema dialect or behavior, invalid schema references and incompatible author-side contract declarations (section 2.1). `IR023` rejects an `independent_of` entry that is not a declared input name of that node. `IR024` rejects a command node with `resources` on a runtime that does not declare `protected_execution` when the namespace sets `require_protected_resources` (issue #45). `IR026` rejects an agent node whose `env` or `secret_env` sets a name or prefix in its harness adapter's `reserved_env` ([protocols.md](protocols.md) section 3), such as the `claude` adapter's `HOME`, `ANTHROPIC_*`, `NODE_*` or `HTTPS_PROXY`, or a variable the runtime assigns to the candidate domain, because the adapter or the runtime owns those variables and they change the harness's destination, its TLS trust or what it and its shell load at start-up (issue #125). `IR027` rejects a CEL expression or path over a static limit (4,096 code points, depth 32, four comprehension macros nested at most two deep), a `matches()` pattern over its size-estimate cap or its group-nesting limit, an expression over the cost budget, or static trigger `params` over the CEL data bounds, including a `maxLength` violation found by schema validation (section 7).

## 11. Worked examples

- [examples/sql-optimization.ir.yaml](examples/sql-optimization.ir.yaml): the canonical graph from `positioning.md`, as written in the DSL example of [dsl.md](dsl.md) section 1: target → baseline → hypotheses → fan-out of a retried subgraph (implement → dev run → equivalence → review → branch → pack candidate by a command node), the fan-out node collecting the candidates → select best (a command node: gain over the baseline, the 15 % threshold, ties broken by the lowest commit) → review bundle narrative (cheap agent) → VCS approval with kora-attached cost → wait for deploy → timer → metrics check. The two scripts, `pack_candidate.py` and `select_best.py`, are command resources of the graph version, delivered and verified by digest (section 4), and belong to the end-to-end fixture of the canonical graph ([protocols.md](protocols.md) section 13); `test_candidate_selection_exact_and_reproducible` covers threshold edge values, ties with that tie-break, the empty set, an invalid baseline, a missing metric, that no reference outside the input set can appear in `best`, and that identical inputs give an identical output digest.
- [examples/dependency-upgrade.ir.yaml](examples/dependency-upgrade.ir.yaml): a small cron graph: Codex upgrades one dependency, a command node runs the tests, an approval on the CLI channel, then a sensor waits for the merge.

Both validate against the schema.

Publication-producing nodes follow [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md): a `VcsRef` is derived from trusted prepared/publication results, not an agent-reported branch. Existing candidate commits are preserved; uncommitted changes become one additional prepared commit. Publication credentials are acquired per purpose by trusted code ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)), not a required node `secret_env`. A published `VcsRef` also records `pushed_by` and `opened_by` as stable host principals. Explicitly granting candidate code a code-host credential grants it independent external side effects.
