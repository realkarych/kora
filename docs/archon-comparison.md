# kora vs Archon

Archon (coleam00/Archon) is the closest open-source system to kora: a graph whose nodes are full coding-agent sessions on the user's own harness. This page compares the two point by point and says which one is stronger on each. Reviewed by the project owner on 2026-10-16.

Archon was checked at release v0.11.1, with the development branch as of 2026-10-15 noted where it changes the picture. kora has no code yet, so every kora cell describes the design, and a kora win is a win on paper; every Archon cell describes shipped software. The sourced version, with the Archon file or document and the kora requirement or decision behind every row, is [knowledge/research/agentic-pipeline-orchestrators/archon-vs-kora.md](../knowledge/research/agentic-pipeline-orchestrators/archon-vs-kora.md).

## Product

| Aspect | Archon | kora (design) | Stronger |
|---|---|---|---|
| Readiness | shipped releases, binaries, Docker image | no code | Archon, by far |
| Target scale | one operator or a small team per install | many teams on one instance | kora |
| One developer, one repository | quick start, ready-made workflow packs | standalone mode is for development only | Archon |
| Scope | software-development workflows plus a chat with the project | the deterministic outer loop around sessions only | tie: different niches |
| Deployment | binary, Homebrew, one container, optional Postgres | standalone process or docker-compose | Archon, because it exists |
| Format stability | breaking changes in almost every minor release | versioned graph format with a published schema | kora |
| Licence | MIT, a move to fair-code is under discussion | Apache-2.0, everything in the open core | kora |

## Features

| Aspect | Archon | kora (design) | Stronger |
|---|---|---|---|
| Graph definition | hand-written YAML, lenient validation | Python compiled to a validated YAML graph format | kora |
| Node kinds | richer set: loop, cancel, include, sub-workflow | branch on CEL, sensor, timer, sub-graph | tie |
| Conditions and trigger rules | four rules, own mini-grammar | seven rules, type-checked CEL | kora, by a small margin |
| Fan-out and sub-graphs | both, child cost rolled up | both, sub-graph with its own policies | tie |
| Retry | node only; every attempt is recorded, but a retry re-runs the node with the same inputs and cannot read an earlier attempt | node and sub-graph; attempts are objects with lineage | kora |
| Loops with feedback | explicit loop nodes, exit by script or output field, retry on rejection; each iteration's outputs are recorded and a node can read the previous one | sub-graph retry with the previous attempt as input | tie: Archon is simpler to write, kora is easier to audit |
| Approval semantics | no timeout, no default, not bound to content | typed form, timeout and default, bound to the commit and the facts | kora |
| Approval channels | CLI, web, Slack, chat adapters, REST | CLI and API, change request, web; chat later | Archon |
| Artifacts | JSON output validated by a schema | domain types with versions and lineage, review bundle | kora |
| Deterministic gates | bash and script nodes, check that the tree is unchanged | command nodes, gate scripts protected from agent edits | tie, kora slightly ahead |
| Cost | as reported by the provider; none for Codex | token ledger, price table, completeness, budgets at every level | kora |
| Provider rate limits and quotas | rate-limit-aware retry, resume after quota reset, fallback model | none | Archon |
| Concurrency per model provider | yes | per runtime, namespace and graph only | Archon |
| Isolation | git worktree per run, not a sandbox | fresh checkout per node, container per attempt | kora |
| Work without a VCS | yes | no | Archon |
| VCS | git only | protocol; a non-git VCS is part of MVP acceptance | kora |
| GitHub integration | GitHub App, bot identity, label and check events with a queue | GitHub App issuance with purpose-scoped credentials and a stable bot principal (ADR-0023); App webhooks after MVP, generic webhook and sensors until then | Archon |
| Runtime | host process only | protocol: local, Docker, adopter runtimes | kora |
| Cron and sensors | macOS launchd interval, no sensors | cron with time zones, sensors | kora |
| Observability | logs, event stream, product telemetry | OpenTelemetry, Prometheus, audit log | kora |
| Operation by agents | skill that also writes workflows, REST API with OpenAPI | MCP servers, skills, parity enforced by a test | kora, Archon close |
| Ready-made pipelines and their distribution | software-development pack, workflow packs from GitHub | one example, no distribution mechanism | Archon |
| Testing a workflow | dry run with pauses at gates, fixtures | dry run on the fake runtime | Archon |
| Web UI | full console | full console in MVP with a reference-grade user-experience bar | Archon, because it exists |
| Chat interfaces | Slack, Telegram, Discord, GitHub mentions | later | Archon |
| Provider credentials per user | own keys and provider subscriptions per user | keys in the environment of the adapter | Archon |

## Technology and architecture

| Aspect | Archon | kora (design) | Stronger |
|---|---|---|---|
| Schema migrations and upgrades | one idempotent schema, no migration history | Alembic, upgrades without downtime | kora |
| Process model | one process | API, stateless scheduler, workers over HTTP | kora |
| Run state | partly on the host's file system | entirely in the database | kora |
| Crash recovery | manual resume | session adoption, leases, at-most-once submission | kora |
| Execution inside a closed network | not possible | a worker needs one outbound HTTPS route | kora |
| Extension model | workflow packs and forge plugins | eleven infrastructure protocols with conformance suites | tie: different axes |
| Harness providers | five working SDK integrations | Claude Agent SDK and Codex app-server in MVP, ACP after MVP, uniform limits | Archon |
| Security defaults | permission checks bypassed, API without authentication | hermetic sessions, tool policy, mandatory tokens | kora |
| Multi-tenancy | none | namespaces with limits and budgets | kora |
| Scale | about ten conversations, five parallel fan-out branches | hundreds of concurrent nodes as a target, not yet proven | tie: nothing to compare yet |

## Summary

kora is stronger on 22 points, Archon on 14, and 7 are a tie; every kora point is a design claim and every Archon point is shipped. kora wins the platform layer: attempts, approvals, artifacts, cost, runtime and VCS protocols, recovery, security, multi-tenancy. Archon wins everything a user touches today: readiness, UI, chat, GitHub integration, ready-made pipelines, provider rate limits and workflow testing. That matches the positioning split (Archon is a workflow engine for a repository, kora is the orchestration platform for an organisation) and shows that kora has to close a large user-experience gap with its MVP console.
