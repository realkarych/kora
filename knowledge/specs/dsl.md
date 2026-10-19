# kora MVP design — Python DSL and compile step

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: FR-1.1, FR-1.4, FR-3.1–3.2, FR-4.3, NFR-6.2, NFR-6.4, NFR-10.3, and FR-0.2 (how an agent authors a graph). The DSL is a client of the IR in [ir.md](ir.md): anything expressible here is expressible there, and nothing here is needed by the scheduler. Owner decisions on design choices are referenced as **DD-n** ("design decision n") and recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md).

## 1. Surface

The example below is the reference DSL example (the functional requirements point here instead of carrying their own sketch, current contract [dsl](dsl.md)): decorators define graphs and subgraphs, `node.*` constructors define nodes, calling a node with other nodes' outputs creates data edges, `>>` creates control edges.

```python
from kora import graph, subgraph, node, approval, expand, branch, fail_attempt, presets, Artifact, TargetKey
from kora.artifacts import VcsRef, CommandResult, ReviewBundle, StructuredOutput

class Hypothesis(Artifact):          # custom artifact type: a Pydantic model
    title: str
    rationale: str = ""
    task_id: str

class MetricsReport(Artifact):
    cpu_seconds: float
    output_path: str

class Candidate(Artifact):
    change: VcsRef
    metrics: MetricsReport
    hypothesis: Hypothesis
    gain_pct: float | None = None

optimizer = presets.claude(model="claude-opus-5-5", skills=["sql-optimizer"], max_events=200, budget_usd=15)
reviewer  = presets.codex(model="gpt-5-codex", skills=["sql-review"], max_events=100)
packer    = presets.registry("packer", version=2)          # platform-team preset, read from .kora/presets/ after `kora presets pull` (DD-25)

@subgraph(max_attempts=3, budget_usd=60, timeout="6h")
def attempt(hypothesis: Hypothesis, baseline: MetricsReport, previous_attempt: PreviousAttempt | None):
    change  = node.agent("implement", preset=optimizer, output=VcsRef, workspace=previous_attempt.change,   # graph repo/rev on attempt 1
                         prompt="Implement {{ inputs.hypothesis.title }} …")(hypothesis=hypothesis, previous_attempt=previous_attempt)
    run     = node.command("dev_run", cmd=["etl", "dev-run", "--task", "{{ inputs.hypothesis.task_id }}", "--json=out.json"],
                           output_file="out.json", output=MetricsReport, workspace=change)(change=change, hypothesis=hypothesis)
    same    = node.command("equivalence", cmd=["etl", "diff-results", "--baseline={{ inputs.baseline.output_path }}"],
                           workspace=change, success_exit_codes=[0, 3])(run=run, baseline=baseline, change=change)
    verdict = node.agent("review", preset=reviewer, output=ReviewVerdict, trigger_rule="all_done",
                         workspace=change)(change=change, run=run, baseline=baseline, same=same)
    accepted = node.command("accept", cmd=["python3", "{resource_root}/pack_candidate.py", "--change=.kora/inputs/change.json",
                                           "--metrics=.kora/inputs/metrics.json", "--hypothesis=.kora/inputs/hypothesis.json", "--out=candidate.json"],
                            resources={"pack_candidate.py": "gates/pack_candidate.py"},
                            output_file="candidate.json", output=Candidate)(change=change, metrics=run, hypothesis=hypothesis)
    branch("decide", "inputs.verdict.approved && inputs.same.exit_code == 0",          # CEL string (DD-3)
           then=accepted, else_=fail_attempt(reason="inputs.verdict.comments"))(verdict=verdict, same=same)
    return accepted

@graph(namespace="dwh_core", repo="dwh/etl", rev="main", schedule="0 6 * * 1", overlap="allow",
       target=TargetKey.from_output("find_target", "target", path="task_id", on_active="defer"),
       runtime="docker", vcs="git", retention={"transcripts": "30d", "artifact_payloads": "30d"},
       protected_paths=["Makefile", "tests/**"])
def sql_optimization(target_task: str | None = None):
    target   = node.agent("find_target", preset=optimizer, output=Target)(requested=target_task)
    baseline = node.command("baseline", cmd=["etl", "dev-run", "--task", "{{ inputs.target.task_id }}", "--json=out.json"],
                            output_file="out.json", output=MetricsReport, attempt_invariant=True)(target=target)
    hyps     = node.agent("hypotheses", preset=optimizer, output=list[Hypothesis])(target=target, baseline=baseline)
    cands    = expand("candidates", attempt, over=hyps, baseline=baseline, max_parallel=3, skip_failures=True)
    best     = node.command("select_best", cmd=["python3", "{resource_root}/select_best.py", "--candidates=.kora/inputs/candidates.json",
                                                "--baseline=.kora/inputs/baseline.json", "--min-gain-pct=15", "--out=best.json"],
                            resources={"select_best.py": "gates/select_best.py"},
                            output_file="best.json", output=Candidate)(candidates=cands, baseline=baseline)
    bundle   = node.agent("bundle", preset=packer, output=ReviewBundle, workspace=best.change)(best=best, baseline=baseline, all=cands)
    ok       = approval("final_pr", channel="vcs", form=HumanVerdict, timeout="3d",
                        binds=dict(vcs_ref="inputs.bundle.facts.change", gate_digests=["inputs.bundle.facts"]))(bundle=bundle)
    deployed = node.wait_for("prestable_rollout", event="deploy.finished",
                             match="payload.commit == inputs.bundle.facts.change.commit", timeout="2d")(bundle=bundle)
    ok >> deployed >> node.timer("soak", "24h") >> node.command("confirm_gain", cmd=[…], output=MetricsReport)(best=best, baseline=baseline)
```

Rules of the surface:

- **Nodes are declared by calling constructors inside a graph function.** The function body runs once at compile time; its return value and local variables are not executed at run time. There is no way to put Python logic on the execution path: conditions are `branch` expressions, loops are subgraph retries, mapping is `expand`.
- **Data edges** come from arguments, and node calls take **keyword-only inputs** (DD-24): `review(change=change, run=run)` binds the outputs to inputs named `change` and `run`; a positional argument to an agent, command, approval, branch or wait node is `DSL003`. Positional arguments are allowed only when calling a subgraph through `expand` or `use`, whose parameters are declared by the subgraph function. Passing `previous_attempt=` declares the `$previous_attempt` input, which is the only way a node receives the previous iteration.
- **Input independence**: `independent_of=["input_name"]` compiles unchanged to IR input names, never producer node IDs. It preserves an existing successful result only; other affected inputs and control edges still invalidate it (FR-6.8a, IR023).
- **Control edges** come from `>>` and `<<`, also with lists (`a >> [b, c]`).
- **Branch targets**: every ordinary target named by `branch` compiles with a direct `depends_on` on that branch, even without `>>`; duplicate edges are deduplicated. One owner per direct target is allowed in MVP (IR010), including a target named in multiple cases of that owner. Routing edges participate in cycle checks (IR001); `$fail_attempt` remains an action. Untargeted joins keep their ordinary dependencies. `attempt_invariant=True` on a branch or any direct target, including an `else` target, fails shared semantic validation with IR010; ordinary non-target invariant nodes remain supported.
- **Conditions are CEL strings** (DD-3, current contract [dsl](dsl.md)): `branch`, retry `predicate`, `approve_when`, event `match` and sensor `until` take the CEL text that lands in the IR unchanged; the compiler checks it with kora's own parser and schema-driven checker, its limits and cost estimate (`IR011`, `IR027`, [ir.md section 7](ir.md#7-expressions-and-templates)) and there is no Python expression builder. Field access on an output (`previous_attempt.change`, `best.change`) yields a **reference** usable for `workspace=` and `binds=`, never a value; using one in a Python `if` or passing a non-string condition is `DSL010`.
- **Outputs** are declared with `output=` (a type, a `list[T]`, or a dict of named outputs); agent nodes always get an implicit `session: AgentSession` output.
- **Artifact types** are Pydantic models subclassing `Artifact`; the compiler emits their exact schemas, parent and declarative behavior identities in authoritative `type_contracts` under `<namespace>/<ClassName>`, without registering over the network (ADR-0018). Schema-backed user models default to the `StructuredOutput` parent unless a more-specific artifact parent is declared; the compiler records the exact parent chain. Built-ins keep their bare names and carry immutable built-in revisions. Compilation includes every parent/reference dependency and writes JSON Schema draft 2020-12 explicitly; custom behavior descriptors are data, never imported graph methods on the execution path. Deployment snapshots this closure independently of current namespace aliases.
- **Workspaces**: `workspace=` takes a repo string, a `VcsRef` output (check out that commit) or nothing, in which case the graph's `repo`/`rev` from `@graph(...)` apply. `@graph(repo=…)` is optional (DD-26); an agent or command node that ends up with no workspace source at all is a compile error `DSL003`.
- **Subgraphs** are functions decorated with `@subgraph`; their parameters are their inputs, their return value their outputs. `expand(name, body, over=, **bound)` maps a subgraph over a list output and returns `List[T]` outputs collected from the body (no fan-in node, ir.md section 4.4); a subgraph used directly as a node is `use(name, body, **inputs)`. A single node passed as `body` is wrapped in a one-node subgraph by the compiler (DD-9).
- **Graph parameters** are the graph function's parameters with type annotations and defaults; they compile to the `params` JSON Schema.
- **Target** (FR-6.11, ADR-0014): `@graph(target=…)` takes `TargetKey.from_param("package", on_active="defer")`, the key read from a graph parameter when the run is created, or `TargetKey.from_output("find_target", "target", path="task_id", on_active="defer")`, the key read from a field of one top-level node output when its complete result is successfully accepted; `on_active` is `"allow"` (the default) or `"defer"` and compiles with the key into `graph.target` (ir.md section 8), which the scheduler binds to `run.target_key`. The first binding is immutable; same-key rediscovery succeeds and a changed key rejects the complete source result (ADR-0019). All source outputs stage until that decision, including exported wrapper results; this creates no body-instance target syntax. A parameter, top-level node, output or scalar path that does not resolve to a string is `DSL014`.
- **Command resources**: `resources={name: path}` on `node.command` declares files the command executes, each path relative to the directory of the graph's module. Nothing is discovered implicitly. The compiler reads the bytes, records each resource and the node's resource-set digest in the IR, and fails with `DSL015` for a missing file, a path outside that directory, a symlink, or a declaration beyond the deploy limits; `cmd` names a resource through the `{resource_root}` placeholder. Resources travel with the graph version, so the target repository needs no copy of them ([ir.md](ir.md) section 4, issue #45). Reuse across graphs stays ordinary Python imports; artifact types keep namespace-scoped names.
- **Prompts and commands** are Jinja2 templates rendered by kora in a sandbox at attempt start (DD-11); the DSL passes them through unchanged and checks that they parse (`DSL012`).
- **Tool permissions**: `allowed_tools=` and `disallowed_tools=` on presets and nodes are sugar that compiles into the node's single `tool_policy` (`allow`, `deny`), the only tool-permission field in the IR (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)); `tool_policy=` may also be given directly.
- **Names and files** (DD-36): a node's id is its `name=` keyword, else the variable it is assigned to; `kora compile graphs/` writes `build/<graph_id>.ir.yaml`; paths in the IR are relative to the directory `kora compile` ran in; the global layer comes from `kora.toml` `[defaults]`.

Agent prompts describe the work and may request local commits; kora publishes a `VcsRef` through the trusted lifecycle in [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md). Code-host publication credentials belong in VCS provider configuration, not automatically in an agent node's `secret_env`.

## 2. Compile step (`kora compile`)

1. **Import** the module in the caller's own process: the `kora compile` CLI process, or the stdio `kora mcp --server authoring` process, both running in the graph author's environment with the author's privileges and the shared compiler library (ADR-0002, amended 2026-10-16). Compilation is client-side by design: no kora process that holds platform credentials (the API, the scheduler, a worker, the session runner) ever imports graph code, and kora imposes no sandbox and no resource limit on the import, because the code is the author's and runs as the author; a module that hangs at import hangs the author's own command and nothing else. Each decorated graph function is called once with placeholder parameters. The compiler itself opens no network: registry presets and the namespace policy are read from `.kora/presets/<name>@<version>.json` and `.kora/namespace-policy.json`, fetched beforehand by `kora presets pull`, so compilation is offline and deterministic (current contract [dsl](dsl.md)).
2. **Collect** the node constructors into a builder; every constructor records `inspect` source location (`file:line`) for NFR-10.3.
3. **Resolve configuration** per node through the layering in section 3 and ask the harness adapter's `check_config` for warnings. A node whose `harness` has no installed adapter has no `check_config` to ask and fails validation in step 5 with `IR018`; in MVP that covers every `acp:<agent>` name, reserved for the ACP adapter that follows MVP (ADR-0004). Compilation **fails** (`DSL013`, DD-32) when a registry preset raises a security-relevant field above what the namespace policy allows: `trust_repo_config`, `permission_mode`, or `mcp_servers` the namespace does not list.
4. **Lower** expressions, edges, subgraph bodies and fan-outs into the IR structures; compute `source_digest` over the module files in sorted order. Read each declared command resource and compute its SHA-256 and the node's resource-set digest (ir.md section 4); the resource content is not written into the IR. When any node declares resources, the compiler also writes the **resource bundle** `build/<graph_id>.resources/`: one file per distinct resource, named by the lowercase hex of its SHA-256, holding its exact bytes. The IR plus its bundle is the complete, immutable compile output; deploying needs neither the graph module nor the original resource files, and a bundle file whose digest differs from its name is a deploy-time error. Emit the used `type_contracts` closure outside `meta`, preserving author-side definitions and immutable behavior identities. Built-in revisions come from the SDK; custom behavior expectations are explicit author-side descriptor data. Compilation reads only local declarations/SDK inputs and the already pulled preset/policy cache. A missing type/descriptor is `DSL004`, not a registry lookup during compilation; deploy additionally verifies the trusted installation can bind the declared behavior.
5. **Validate** the result with the same validator the API uses (ir.md section 10), so compile errors and validate errors are one vocabulary. The one step compile omits is the server-side compile of each CEL expression with the binding: `kora compile` never imports the binding, so it runs without the `kora[cel]` extra ([ir.md section 7.2](ir.md#72-parser-limits-and-canonical-rendering)). A CEL fault carries the validator's `IR011` or `IR027` with its `yaml_path` and `offset`.
6. **Emit** YAML with schema key order, 2-space indent, block scalars for multi-line prompts, no anchors, LF endings. Byte-identical output for identical sources and identical declared SDK/cache inputs (NFR-6.2) is a future compiler contract. The existing design-stage CI validates the committed example documents against the IR schema; the compiler is not implemented yet.

The compiled document is the only thing that reaches the control plane: `POST /validate` and deploy accept it and execute no Python ([api-and-agent-surface.md](api-and-agent-surface.md) sections 2 and 7). Two tests hold the boundary. `test_control_plane_never_imports_compiler` checks over the module graph that the API, scheduler, worker and runner packages do not import the compiler's import step. `test_parity_client_side_operations` checks that the OpenAPI document contains no operation that compiles sources while `kora compile` and `kora_compile` exist.

Compile errors are structured (FR-0.4): `{code: "DSL003", message, location: {file, line}, node, hint}`. Codes: `DSL001` graph function raised, `DSL002` duplicate node name, `DSL003` unbound input or workspace (a positional argument to a node, an argument that is not a node output, parameter or literal, or a node with no workspace source), `DSL004` output type unknown or not an `Artifact`, `DSL005` cycle introduced by `>>`, `DSL006` `previous_attempt` outside a retried subgraph, `DSL007` secret value literal detected in config (looks like a token), `DSL008` preset not found (in code or in `.kora/presets/`; hint: `kora presets pull`), `DSL009` merge conflict that no rule resolves, `DSL010` a reference used as a Python boolean or a condition that is not a CEL string, `DSL011` unknown field in a preset or node config, `DSL012` Jinja2 template does not parse (a CEL fault is reported with the validator's `IR011` or `IR027`), `DSL013` a registry preset raises `trust_repo_config`, `permission_mode` or foreign `mcp_servers` above the namespace policy (DD-32), `DSL014` a `TargetKey` names a parameter, node, output or path that does not resolve to a string (the compile-time form of `IR020`), `DSL015` a declared command resource is missing, a symlink, outside the graph module's directory, or beyond the deploy limits (at most 64 files and 1 MiB per graph version, 256 KiB per file).

Three file classes (NFR-6.4) are reflected in the recommended layout: `graphs/*.py` and `presets/*.py` (human-authored), `build/*.ir.yaml` and `.kora/presets/*.json` (generated or pulled, committed, never hand-edited), `.kora/secrets.*` (ignored by VCS, referenced by alias only).

## 3. Preset layering and merge rules (FR-3.2)

Layers, lowest to highest precedence: **global** (deployment config, platform team) → **graph** (`@graph(...)` keywords) → **preset** → **node** (constructor keywords). Presets come from two places (DD-25): Python objects in graph code (`presets.claude(...)`, which may extend another preset with `base=`) and the platform registry (`presets.registry("packer", version=2)`, published through the API and pulled into `.kora/presets/` by `kora presets pull`). Either way the preset is resolved at compile time and a snapshot is copied into the IR under `meta.presets`, so the scheduler never consults the registry. The result is written into the IR node `config`, `retry` and `limits`; the global and graph layers are snapshotted under `meta.defaults` for provenance.

| field group | fields | merge rule |
|---|---|---|
| scalars | `harness`, `model`, `permission_mode`, `sandbox`, `trust_repo_config`, `runtime`, `vcs`, `prompt`, `output_file`, `timeout`, `max_events`, `budget_usd`, `stall_after`, `strategy`, every retry number | higher layer overrides |
| append lists | `skills`, `tool_policy.allow`, `tool_policy.deny` (into which `allowed_tools` and `disallowed_tools` are folded), `subagents` (by `name`), `retry_on`, `non_retryable`, `network_policy.egress`, `protected_paths` | concatenate in layer order, dedupe keeping first occurrence; there is no removal syntax, a layer that needs a shorter list declares a different preset (current contract [dsl](dsl.md)) |
| keyed maps | `mcp_servers`, `env`, `secret_env`, `hooks` (per event) | deep merge by key; a higher layer's value for the same key replaces the whole entry; no delete syntax |
| text append | `system_prompt_append` | higher layers append with a blank line between |
| structured | `output_schema`, `form`, `workspace` | replaced whole; no partial merge |
| conflict | `tool_policy.allow` and `tool_policy.deny` naming the same pattern | the higher layer wins; same layer → `DSL009`; at run time the namespace policy is prepended and a namespace deny always wins |

Two fields are **not** inheritable: `prompt` is always node-level in practice (a preset may set a default template, but `DSL011` warns), and `workspace` must be explicit or default to the graph's repository. `trust_repo_config` is inheritable by requirement (FR-3.4), and `kora compile` prints the resolved value for every agent node in its summary, because it is security-relevant.

Precedents: gh-aw `imports` merge table (lists concatenated and deduped, maps deep-merged, scalars main-wins), Airflow `client_defaults`. Kestra's removal of `pluginDefaults` is the warning that implicit layering becomes a support burden: this is why every rule is in one table and the IR carries the resolved result.

## 4. Testing graphs without model calls (NFR-10.1)

`kora plan build/sql.ir.yaml --param target_task=x` prints the materialised plan: node order, trigger rules, resolved config digests, which fields each harness cannot honour. `kora.testing.compile_graph(fn)` returns the IR dict for unit tests. Neither shows behaviour: retry, branching, fan-out and approval are tested with **scenarios** (issue #46).

### 4.1 Scenarios

A scenario file `tests/scenarios/<graph>/<name>.yaml`, validated by [scenario.schema.json](scenario.schema.json), describes everything a run would receive from outside: `params`, the outcome of each agent execution, overrides of command nodes that need external systems, approval responses, inbound events, explicit public operations such as a retry, and an `expect` block. Outcomes are hand-authored: a structured output, a `change` as files that becomes a real commit, an error class, and usage. Raw SDK recordings test adapter normalisation in the harness conformance suite, and live-model evaluation tests response quality; neither belongs to a scenario.

**Selectors** address an execution independently of execution order. A selector is a path of segments separated by `/`:

| segment | meaning |
|---|---|
| `implement` | a node id at the current level |
| `candidates[1]` | item 1 of the fan-out `candidates`, by position in its `over` list; that list comes from scenario-supplied outcomes, so positions are fixed before execution |
| `#2` | suffix on a sub-graph node or a fan-out item: its sub-graph attempt 2 |
| `@3` | suffix on a node: its runtime attempt 3 |

For example, `candidates[1]#2/implement@1` is the first runtime attempt of `implement` in the second sub-graph attempt of fan-out item 1. An omitted sub-graph attempt or runtime attempt means 1. A selector names the execution of an agent node, a command node, a human-input node or a wait.

**Actions** are armed in file order: each fires at most once, and an action is armed only after every earlier action has fired, so a sequence such as reject, retry, approve is deterministic. An armed `when: {waiting: <selector>}` fires once the addressed human-input or wait node has a currently open activation, which after a retry is the new activation, not the closed one. An armed `when: {settled: true}` fires once the run is terminal, or once all of these hold together: no claimable work, no open approval or wait, and execution quiescence, meaning no attempt claimed or running, no real subprocess alive, and no preparation, publication or result report in flight. A real command that is still running therefore holds `settled` until its result is recorded, whatever the worker and process scheduling. An action still armed when the run ends is reported as unused. `do` is one of `respond` (an approval response through the ordinary approval API: `response` is forwarded unchanged as the API's `response` and validated against the form of the addressed node, so a graph-specific form with fields such as `rollout_percent` takes its own fields; the engine adds the bound SHA and digests from the open request as the CLI does, and `reviewer` stays outside the response as the fake VCS reviewer reference), `retry` (the public retry operation on a node), `push` (a commit to the candidate branch bound by the open approval) and `event` (an inbound event through webhook ingress). A string value of the form `$from: <selector>.<output>.<path>` is resolved from that artifact when the action fires, so an event can name a commit the scenario cannot know in advance. In `expect`, `not_run` means the node never executed and never opened: it had no runtime attempt and, for a human-input or wait node, never opened a wait. A node blocked by its dependencies counts, whether it stayed `pending`, was never materialised, or ended `upstream_failed` or `skipped`.

**Strictness.** Every supplied outcome, override and action is consumed exactly once. The scenario fails as an **execution error** in four cases: an agent execution without a matching outcome; an outcome or action that matches no execution; two entries for one selector; a selector matching two executions. Only an entry marked `optional: true` may stay unused. Overrides are explicit per selector (`override: {exit_code, output}`). No gate or external command is overridden implicitly.

**Results.** `kora test --json` and the stdio authoring tool return one record per scenario with `status`:

- `passed` — every expectation held;
- `failed` — an `expect` did not hold;
- `error` — the scenario could not be executed as written.

A scenario expecting `run: failed` that gets it is `passed`. The record lists unused or missing selectors, each expectation with expected and actual values, the waits still open at the end, and every command node marked `executed` or `overridden` with its exit code.

### 4.2 The engine, isolation and time

A scenario run is `kora standalone` wired through configuration:

- the fake harness reads outcomes from the scenario;
- the scenario secrets provider supplies only values the scenario declares;
- approvals, events and public operations go through the ordinary API;
- time and randomness are injected.

The API, database, scheduler, worker and provider paths are the ordinary ones; there is no second execution engine (NFR-0.1). The VCS is selected by configuration: the independent directory-backed VCS by default, or a local Git repository with an in-process code-host fake for Git-specific scenarios. Scenarios refer to revisions and change requests through provider-neutral references and run ordinary VCS plans; recorded publication events never replace real preparation and publication.

**No production secrets and no network, enforced.**

- `kora test` refuses a configuration that names any secrets provider other than the scenario provider. The run rebinds every secret alias of the graph document (`secrets.aliases`) to the scenario provider by alias name, whatever provider the deployment reference names; the scenario's `secrets` map supplies dummy values, and resolving an alias it does not declare is an execution error. No value is read from the host environment.
- Commands run on a runtime that enforces the absence of network: the `docker` runtime with no network, or the `local` runtime inside a network namespace where the host allows unprivileged ones.
- Each command's record reports `network: enforced` or `network: unenforced`, and `filesystem: isolated` or `filesystem: shared`. `--strict`, the default in CI, fails on `network: unenforced`. A macOS host without Docker cannot enforce it and says so.
- **What is guaranteed and what is not.** The secrets guarantee is that no production secret is resolved: the scenario provider is the only one loaded. It does not stop a command from reading files on the host. Only a runtime with filesystem isolation (`docker`, reported `filesystem: isolated`) keeps host credentials out of reach; on `local` (`filesystem: shared`) a command runs as the author's user, as ADR-0016 states for `local`.

**Virtual time** advances only when the engine is quiescent: no real subprocess running, no claimable work, and every remaining step waiting on a timer. It then jumps to the next `wake_at`; it never advances while a command runs.

**Watchdog.** Each real command has a real-time limit, 60 seconds by default and configurable per scenario. On expiry the process group is terminated, the scenario ends with `error: watchdog_expired`, and every child process group is released before the result is returned.

### 4.3 Entry points

`kora test <graph module>` runs every scenario of the graph in the author's own process, like `kora compile`; it is not an API operation (ADR-0002). Unlike compile, it starts an in-process `kora standalone`, which evaluates CEL, so `kora test`, `kora_test` and `kora.testing.run_scenario` require the `kora[cel]` extra and refuse to start without it ([ir.md section 7](ir.md#7-expressions-and-templates)). The stdio authoring MCP server exposes the same runner as `kora_test`; the HTTP-mounted server omits it because it would import graph sources. `kora.testing.run_scenario(...)` returns the same result to pytest. `kora trigger --dry-run` keeps its earlier meaning: it runs the graph on the fake runtime and fake harness, replaying recorded event files declared per node in `tests/fixtures/<graph>/<node>.jsonl`. Behavioural assertions belong in scenarios.

The future executable contracts for this section are in [protocols.md](protocols.md) section 13, "Scenario contracts"; documentation CI validates scenario files against the schema and executes none of them.

## 5. How an agent authors a graph through the skill (FR-0.2)

The `kora-author` skill (api-and-agent-surface.md section 5) runs in the **user's own** coding-agent session, with the user's token, and drives the agent through the same loop a human uses, with the `kora-authoring` MCP server's tools as the only interface (DD-5, DD-28):

1. Call `kora_reference` for the DSL, the registry presets of the namespace and the existing artifact types.
2. Write `graphs/<name>.py` using keyword-only inputs, CEL strings for conditions and only presets that exist, never inventing harness flags; declare custom artifact types as `Artifact` subclasses.
3. Call `kora_compile`, a tool of the stdio authoring server that compiles in the author's own process (api-and-agent-surface.md section 4), with the graph source files; on `DSLnnn` errors edit the named `file:line`; on `IRnnn` errors read `yaml_path` and map it back through the `source` field of the node.
4. Start `kora_plan` (an MCP Task) with representative params; check the warnings list for unhonoured fields and the resolved `trust_repo_config` and `tool_policy` values.
5. Call `kora_deploy`; record the returned graph version id in the response to the user.
6. Only when the task asks for it, switch to the `kora-operating` server: `kora_trigger` (a task), then `kora_runs`, `kora_inspect` and `kora_cost`, and report run id, status and estimated cost with its completeness. Approvals need a token with `approve`; the default MCP token has none (DD-32).

The skill also states what the agent must not do: hand-edit `*.ir.yaml`, embed secrets, loosen `trust_repo_config` or `permission_mode` beyond what the task asks, or respond to approvals the user did not ask it to answer. The Codex variant carries the same steps as a Codex skill (DD-20).

## 6. Determinism and versioning of the DSL

The DSL package version is written into `meta.compiler`; the DSL emits the newest IR minor version it knows. A graph compiled with an older DSL stays deployable as long as its IR major version is supported; `kora migrate-ir` (NFR-6.3, post-MVP) rewrites older minors in place. The DSL never emits fields the schema does not know, so `kora validate` on the DSL's own output is always clean, which is a CI test over the example graphs.
