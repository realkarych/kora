# kora — Product Positioning

Status: Reviewed by the project owner on 2026-10-16. Decisions in this document were taken by the project owner; research backing is in `knowledge/research/`, which also holds the dated figures behind every statement about competitors here (versions, popularity, release dates).

## One-liner

**kora is an open-source platform for running graphs of coding-agent sessions at organisation scale.** The unit of work is a full Claude Code or Codex session with its own skills, subagents, hooks and MCP servers. The platform adds what neither a single session nor a single-repo workflow engine provides: a scheduler-grade control plane (cron, sensors, namespaces, hundreds of concurrent sessions), attempts and artifacts as first-class objects with lineage, approvals bound to content, and protocols that let it run on an organisation's own runtime, VCS and secrets infrastructure.

Tagline: **Orchestrate coding-agent sessions as a graph.**

"Airflow for agentic pipelines" stays as the one-line analogy for people who know Airflow; it is not the tagline.

## The problem

Teams already run many coding-agent sessions to do real engineering work: periodic query optimisation, dependency upgrades, test-flake repair, migrations. Every published production pipeline of this kind (Google, Airbnb, Anthropic, Dropbox, Stripe, Uber) has the same shape: deterministic targeting, bounded per-unit agent attempts, mechanical validation gates, batched human review, slow rollout. Every one of them was built bespoke.

The tooling market splits into four layers that do not meet:

1. **Vendor cloud sessions** (Claude Code Routines, Codex automations, Cursor Automations, Google Jules scheduled tasks, Devin). One prompt → one session → one PR. No DAG, no approval between stages, no typed artifacts, no cross-run cost view, one vendor's agent only.
2. **Workflow orchestrators** (Airflow with its `common-ai` provider, Prefect, Dagster, Kestra, Flyte, Temporal). Mature DAGs, retries and human-in-the-loop. Most model an "agent" as an LLM tool loop inside one task. The exception is Airflow's `AnthropicAgentSessionOperator`, which runs a whole server-side **Managed Agents** session, but not a local coding harness and not a repository checkout. Kestra documents retrying a sub-flow as a unit and, in a newer mode, a whole task group. None of them has a session handle, a workspace or a change-request as a first-class object.
3. **"Run many Claude Codes" tools** (Gas Town, Paperclip, Claude Squad, Conductor and hundreds of others). Mostly tmux, worktree and kanban managers. The exception, and the closest competitor, is **Archon** (coleam00/Archon, MIT): a TypeScript/Bun YAML workflow engine whose nodes are Claude Agent SDK, Codex SDK, Pi and other provider sessions plus bash/script nodes, each run in its own git worktree, with `depends_on`, trigger rules, `fan_out`, included sub-workflows, node-level retries by error class, loop nodes, approval gates, per-node token and USD cost, PR creation on GitHub (GitLab and Gitea adapters receive webhooks and post comments), GitHub-webhook triggers, a web console, Slack/Telegram entry points, and single-container deployment on SQLite. It is SDLC-shaped (issue → plan → implement loop → tests → review → approve → PR) and positioned as "n8n for software development".
4. **Software factories** (LangChain Open SWE, Stripe Minions, Dropbox Nova, Factory.ai). End-to-end autonomous coding agents with a fixed pipeline: investigate, change, validate, open PR, react to review, babysit CI. Open SWE is the closest open-source one; it is built on Deep Agents, has no user-defined graph, no stage gates, no retry configuration and no multi-harness notion, and is currently closed to external contributions.

Archon does ship a user-defined DAG of coding-agent sessions on the user's own harness. What nobody ships, Archon included, is the orchestration layer around it: attempts addressable as inputs, with per-attempt artifacts and lineage, retry of a whole sub-graph as recorded attempts with the previous attempt's outputs as inputs (Archon's loop nodes repeat a group of nodes with feedback and record every iteration, but a node can read only the previous iteration and no earlier attempt is addressable as an input), approvals bound to a commit SHA and gate digests with timeout and default, domain-typed artifacts (change request, metrics report, evidence bundle) on edges, a scheduler with cron and sensors, pluggable runtime, VCS and secrets protocols for enterprise environments, and a code-first DSL compiled to a validated IR. The phrase "Airflow for agents" is unclaimed as a brand.

## The thesis: a determinism layer over an agentic layer

A coding agent is excellent at open-ended, non-deterministic work inside a bounded task: research a codebase, form hypotheses, try a change, fix review comments. It is bad at being the control loop: it forgets, over-spends, retries blindly, and cannot be audited.

kora keeps the two apart:

- **Outer loop is deterministic and owned by kora**: graph topology, dependency rules, retries, timeouts, approvals, budgets, artifacts, scheduling, and cheap mechanical gates (build, tests, benchmarks) as command nodes. No model in the coordination loop.
- **Inner loop is the harness's own**: subagents, tool permissions, compaction, skills. kora does not re-implement it; it configures it per node and consumes its result.

This is the same split Anthropic ("workflows vs agents"), 12-factor agents and the published case studies converge on. kora is the reusable version of the outer loop.

## AI-first

kora is operated by agents as much as by people. Everything a human can do through the UI, CLI or API, an agent can do through kora's own MCP server and a published skill set: author a graph in the DSL, compile and validate it with machine-readable errors, deploy it, trigger a run, inspect runs, logs, artifacts and cost, answer an approval, retry a node, cancel a run, register a preset. The API is the single source of capability; UI, CLI, MCP tools and skills are four equal clients of it, and a capability that exists in one must exist in all. Consequences for the design: every operation is idempotent and addressable by stable ids; every error is structured; every artifact is readable by an agent without the UI. The reverse does not hold: an agent session running as a node inside a graph has no access to kora; the MCP server and the skills exist for the engineer's own session, which operates kora with the engineer's token and permissions.

## Who it is for

Primary users (decided 2026-10-15, replacing the earlier "one engineer" persona after the Archon assessment):

- **A platform team** that installs kora once for an organisation, implements the runtime, VCS and secrets protocols for its infrastructure, publishes node presets, and operates the control plane.
- **Engineers in product and data teams** (data engineers, frontend, DevOps) who author graphs in Python on those presets and own the outcomes of their pipelines. They never operate the platform.

A single engineer can still run `kora standalone` on a laptop, but that is a development convenience, not the design centre. For a single developer on GitHub with one repository, Archon is the better tool today and kora does not try to beat it there.

Distribution: **public open source, Apache-2.0**, general purpose. Every protocol and reference implementation is in the open-source core; adopters (including the owner's own organisation, which runs an in-house CI and a non-git VCS) plug in their own infrastructure without forking.

## What makes kora different

| Capability | Vendor cloud sessions | Orchestrators | OSS multi-agent tools | kora |
|---|---|---|---|---|
| Node = full coding-agent session | yes (single) | no (LLM loop in a task) | yes | **yes, as a graph node** |
| Harness-agnostic (Claude Code and Codex in MVP; ACP agents such as OpenCode, Pi, Gemini after MVP) | no | n/a | partly | **yes** |
| Persisted DAG with dependency rules | no | yes | rarely | **yes** |
| Approval gate between stages, typed, with timeout and default | no (per-tool only) | yes | rarely | **yes, pluggable channel** |
| Retry policy per node and per sub-graph | no | node only | rarely (node only) | **node and sub-graph** |
| Typed artifacts on edges (structured output, git ref, session) | no | state / XCom | PRs, text | **yes, extensible** |
| Deterministic gate steps (build, tests, benchmark) outside the agent loop | no | yes | rarely | **yes, command nodes** |
| Pluggable runtime (local, Docker, sandbox, K8s, enterprise CI) | no | yes | partly | **yes, protocol** |
| Pluggable VCS (git and non-git) | git only | n/a | git only | **yes, protocol** |
| Cost accounting per node / run | partial | rarely | partial | **tokens + price table, soft USD cap** |
| Code-first DSL compiled to a versioned IR | no | partly (Flyte; Airflow serialises DAGs) | no | **Python → YAML IR** |
| User-defined pipeline (vs fixed factory pipeline) | no | yes | partly | **yes** |
| Agent-operable with full parity (MCP server + skills for every human operation) | partly (CLI) | no | partly (Archon ships a skill that runs and writes workflows, no MCP server) | **yes, by principle** |

Archon gets its own point-by-point comparison, with the stronger side named on each point, in [archon-comparison.md](archon-comparison.md), because it is the only system in the third column with a real DAG.

## Primary contrast: Archon

kora positions first against Archon, because it is the only other open-source system where a graph node is a full coding-agent session on the user's own harness, and because anyone evaluating kora will find Archon first. Archon's model: a hand-written YAML workflow per repository, run in a git worktree on one host, with agent, bash and loop nodes, an approval gate and a PR at the end; a loop exits on a script's exit code, an output field or the agent's own signal, and an approval is not bound to the content it approves. It is the right tool for one developer encoding one repository's SDLC, and kora concedes that ground.

kora's model: a platform. Graphs are Python compiled to a validated IR and registered in a control plane that schedules them on cron, webhooks and sensors across teams and runtimes. The split in one sentence: **Archon is a workflow engine for a repository; kora is the orchestration platform for an organisation.** The differences, ordered by how hard they are to copy:

- *It runs on the organisation's infrastructure.* Runtime, VCS and secrets are protocols with conformance suites: local and Docker in the core, cloud sandboxes and Kubernetes next, and enterprise CI systems and non-git VCS by the adopter. Archon runs git repositories in worktrees on its own host.
- *It is a control plane.* Namespaces, concurrency limits, cron and sensors, Postgres, crash adoption, hundreds of concurrent sessions across dozens of teams. Archon runs on one host for a single developer or a small team.
- *Attempts are objects.* A failed sub-graph is retried as a unit with the previous attempt's artifacts and feedback as inputs, every attempt's artifacts are kept with lineage, and the cost of all attempts is attributed to the accepted change. Archon records attempts and iterations, but retries at node level only and does not make an earlier attempt addressable as an input.
- *Approvals are bound to content.* A kora approval records the commit SHA and the gate digests it covers; a new push invalidates it. Archon's approval is not bound to a commit or to content.
- *Artifacts have domain types.* Change request, metrics report, command result, session, evidence bundle: typed edges the scheduler can branch on and the reviewer can read. Archon passes JSON-schema outputs.

Coexistence needs no dedicated integration: a command node can call Archon's REST API, so a team that already encoded its SDLC in Archon can schedule and gate those workflows with kora. Ideas kora borrows from Archon are listed in `knowledge/research/agentic-pipeline-orchestrators/archon.md` (error-class-only retry decisions, `outcome_field`, attention forms readable by the next node (in Archon's development branch), `none_failed_min_one_success` trigger rule, `--dry-run` without providers, declared-only skills and MCP per node, installable workflow packs).

Secondary contrasts:

- **vs vendor cloud sessions** (Claude Code Routines, Codex automations, Cursor Automations, Jules): one prompt → one session → one PR, one vendor, no DAG, no approvals between stages, no cross-run cost view. kora chains them, gates them, retries the failed stage, attributes cost, and runs on any harness and runtime.
- **vs Airflow `common-ai` / Prefect / Dagster**: they orchestrate LLM calls inside data tasks; kora orchestrates sessions that touch repositories and produce PRs. An Airflow task can start a kora run through the webhook trigger and a command node can call Airflow's API; kora does not replace Airflow for ETL.
- **vs LangGraph / Temporal style graphs**: those are libraries for code a team writes, a graph of Python functions with checkpoints and interrupts or deterministic workflow code with durable steps; the session lifecycle, workspace, VCS, approvals, evidence and cost accounting are assembled by each team on top. kora nodes are whole sessions, and that outer layer is the product.
- **vs Gas Town / Paperclip**: those coordinate long-lived agent "workers" with chat and merge queues; kora is a scheduler with a persisted graph, typed edges and audit, not an org chart.

## Canonical use case

Recurring optimisation of SQL queries in ETL tasks of an Airflow-like data-pipeline framework running on a MapReduce cluster. Today a human does it like this: run the ETL task in dev mode against production tables (writing to a dev area) and record all operation metrics as the **baseline**; form hypotheses about individual pieces of the query; measure the pieces; assemble the final query and run the same dev-mode task again; the **diff between the two dev runs is the profit or loss**. The result is a PR.

As a kora graph the process is a background job with three human touchpoints: triggering a run, approving the final PR, and handling system errors. The engineer usually names the target task when triggering the run; if they do not, the graph picks one.

1. *Find target* — if the trigger names no target, an agent session scans the pipeline and picks a slow task; either way it studies the code and the task statement. Output: structured target.
2. *Baseline* — the task runs in dev mode and its operation metrics are recorded, either by a command node or by an agent session through the ETL framework's MCP tools and skills. Output: `MetricsReport` (baseline).
3. *Hypotheses* — agent session proposes N approaches. Output: list of hypotheses.
4. *Fan-out* — one sub-graph per hypothesis: an agent implements it on an isolated checkout, runs the candidate in dev mode and checks result equivalence against the baseline with the framework's MCP tools and skills, which run and compare the queries deterministically; a separate review session, which did not write the change, examines the diff, the metrics and the equivalence evidence and either accepts the attempt or fails it with a reason. The sub-graph retries with the previous attempt's artifacts and feedback as inputs until it succeeds or its attempts, budget or timeout are exhausted.
5. *Select* — a command node applies a fixed formula to the fan-out's typed list: each candidate's gain over the baseline, the configured threshold, the largest gain with a defined tie-break; the candidate records it chooses from were packed by a command node as well, so no model copies a number or a reference (amended 2026-10-16, [architecture critique, finding A05](../knowledge/reviews/architecture-critique-2026-10-16.md#a05-deterministic-selection), issue #17). An author whose selection needs judgement uses an agent node on a cheap model instead, and a command node verifies its choice.
6. *Review bundle and PR* — kora attaches the evidence to the PR: facts it recorded itself (diff, baseline and candidate metrics with the provenance of each number, that is whether a command node measured it or an agent session reported it, gate results, cost of all attempts) and the agent's narrative (why this candidate), rendered apart. The human's only job is to read it and approve; the approval is bound to the commit and the facts.
7. *Rollout to pre-stable* — the PR is merged and deployed by the organisation's usual process, outside kora; a node waits for the deploy signal, then a timer and a metrics check confirm the gain on real load.

**Success criterion** for this graph is explicit and checkable: the candidate produces equivalent results and improves the recorded metrics by at least the threshold, confirmed after rollout. The numbers come from tools that measure deterministically (the domain's MCP tools and skills, or command nodes), not from a model's judgement, and the attempt is accepted by a check outside the session that made the change. The graph author decides how strict that check is: a separate review session, a command node, or both.

**What proves kora against Archon** is not this graph on Docker and git (Archon can approximate that) but the same graph running unchanged on a second runtime and VCS implemented through the protocols (the owner's organisation's in-house CI and non-git VCS), with sub-graph retry, content-bound approvals and attempt lineage intact. That is the MVP acceptance test (see `docs/requirements/functional.md`).

### Other workloads the owner's teams want on kora

- **Propagating a field from the raw layer to DWH data marts**: canonicalisation, sampling, test runs, deterministic checks, PR.
- **ETL graph analysis**: find duplicated sub-graphs or processes across hundreds of DAGs and propose a more efficient restructuring; this is optimisation at the graph level, not the task level.

kora itself is domain-agnostic. A frontend or DevOps engineer should be able to build their own graph (dependency upgrades, flaky-test repair, lint migrations) from the same node kinds and protocols; the data-engineering examples are the owner's own pilots, not the product boundary.

Everything above the node level is deterministic and visible; everything inside a node is the agent's.

## Non-goals

- **Not a framework for writing agents.** No LLM-call graph, no tool loop, no prompt library. The harness owns the inner loop.
- **Not a general task runner.** Command nodes exist for mechanical gates around agent sessions, not for running arbitrary Python pipelines; that is what data orchestrators are for.
- **Not a sandbox provider.** Isolation comes from Docker, cloud sandboxes, Kubernetes or an enterprise runtime through the runtime protocol.
- **Not a data orchestrator.** kora does not schedule ETL; it interoperates with Airflow and friends.

## Risks and watch-list

- Vendors absorb pipeline features into their harnesses (Claude Code dynamic workflows, Managed Agents workflow runs). kora's defence: cross-harness, cross-run, approval-gated, cost-attributed composition that a single-vendor runtime cannot offer.
- Harness surfaces drift (SDK options, event schemas, protocol versions). kora fixes harness versions per installation, wraps the vendor SDKs (and, after MVP, ACP) behind a harness adapter protocol, and checks tested version ranges at installation health check.
- Prefect, Dagster or Airflow ships a session-level node first. kora's defence: harness-agnostic, VCS-agnostic, runtime-agnostic from day one.
- Human review remains the throughput ceiling: AI-authored PRs wait several times longer for review than human ones. kora's answer is to reduce the human touchpoints to one evidence-backed PR approval per target (the review bundle), not to produce more PRs.
- Software factories (Open SWE, Factory.ai) may add user-defined graphs and multi-harness support, collapsing the gap from the other side.
- Archon moves fast (thousands of commits, frequent breaking YAML changes) and could add sub-graph retry, content-bound approvals, cron and remote runtimes before kora's MVP ships. kora's defence is the scheduler-grade layer and enterprise protocols, not the node model.

## Evidence, hypotheses and open risks

What is backed by evidence today:

- **The problem is real for the owner.** The owner personally runs several dozen agent-driven tasks; their team's Airflow-like ETL framework serves ~300 authors per week, ~400 graphs and tens of thousands of scheduled processes; each DWH has ~5 data engineers/analysts developing ETL full time, and there are several dozen such DWHs. The SQL optimisation routine described above is done by hand today.
- **The pipeline shape is proven elsewhere.** Google, Airbnb, Anthropic, Dropbox, Stripe and Uber all built the same deterministic-outer-loop shape bespoke (see `knowledge/research/agentic-pipeline-orchestrators.md`).
- **The gap is narrower than first surveyed.** Archon already offers a user-defined YAML DAG of coding-agent sessions on the user's own harness with worktrees, approvals, node retries and PR creation. What remains unserved is the orchestration layer listed above: attempt lineage and sub-graph retry, content-bound approvals, domain-typed artifacts, cron/sensor scheduling, and pluggable runtime, VCS and secrets for enterprise environments.

What is a hypothesis, not yet validated:

- **Hypothesis "external demand": demand beyond the owner's organisation.** Other platform teams want an organisation-scale orchestration layer with enterprise protocols enough to adopt a new control plane, rather than running Archon per repository. Test: start conversations with prospective adopters once the second slice runs. Use the earlier pilot to count organisations (not individuals) that implement a protocol or run more than one team on one kora instance. See the [approved milestone order](decisions/0001-own-control-plane.md#milestone-order-amendment-of-2026-11-11). Archon's popularity validates the category; it does not validate the platform layer.
- **Hypothesis "multi-harness value": mixing harnesses per node has value.** Test: in the pilot, measure how often a graph uses more than one harness and whether it changes outcomes or cost.
- **Hypothesis "own scheduler is necessary".** The alternative is an existing engine (Temporal, Kestra, Airflow) plus an adapter that runs a whole coding session as one task. A desk analysis ([knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md](../knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md)) found the hypothesis partly confirmed: an own scheduler is not strictly necessary, but no engine removes kora's own state model of attempts, artifacts and content-bound approvals. The resulting decision, an own control plane with run state kept so that a Temporal backend stays possible, is [ADR-0001](decisions/0001-own-control-plane.md).
- **Hypothesis "outer-loop value": the value is in the outer loop, not in candidate generation.** Harnesses will keep absorbing single-session parallelism (dynamic workflows, Managed Agents, Cursor subagents). Test: pilot metrics below.

Pilot metrics (to be collected on 20–30 real targets before the first public release): accepted useful changes per target; human minutes per accepted change; full cost per accepted change including failed attempts and fan-out losers; maintenance effort of the graph and the platform; comparison against the same work done by hand or via a vendor scheduled session.
