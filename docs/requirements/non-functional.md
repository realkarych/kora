# kora — Non-Functional Requirements

Status: Reviewed by the project owner on 2026-10-16. Targets marked **[MVP]** apply to the first usable release; **[target]** are design constraints the MVP must not block.

Run-admission amendment dated 2026-10-17: direction and implementation plan accepted by the project owner; wording pending line-by-line owner review. See [ADR-0020](../decisions/0020-execution-capacity-and-bounded-run-acceptance.md).

Immutable-history amendment dated 2026-10-17: direction and implementation plan accepted by the project owner; wording pending line-by-line owner review. See [ADR-0018](../decisions/0018-immutable-attempt-history.md).

Execution-recovery amendment dated 2026-10-17: direction and implementation plan accepted by the project owner; document wording pending line-by-line owner review. See [ADR-0017](../decisions/0017-execution-uncertainty-and-safe-replacement.md).

Publication-boundary amendment dated 2026-10-17: pending owner review; architectural direction recorded in [ADR-0016](../decisions/0016-trusted-execution-and-publication.md).

External-observation amendment dated 2026-10-17: direction and implementation plan accepted by the project owner; wording pending line-by-line review. See [ADR-0021](../decisions/0021-external-observations-outside-transactions.md).

Component-logging amendment dated 2026-10-18: direction and implementation plan approved by the project owner under [issue #91](https://github.com/realkarych/kora/issues/91); NFR-9.4 wording accepted for merge by the project owner in [PR #93](https://github.com/realkarych/kora/pull/93) on 2026-10-18. The [dated research](../../knowledge/research/orchestrator-logging-patterns-2026-10-18.md) and [planning review](../../knowledge/reviews/structured-logging-plan-review-2026-10-18.md) are evidence, not runtime verification.

## 0. Design centre

- **NFR-0.1** The platform persona is the design centre: every requirement below is written for an instance shared by many teams on an organisation's own infrastructure. Standalone mode (NFR-7.1) is a development convenience and must never constrain the platform design (no feature may exist only in standalone mode; nothing in the data model may assume a single tenant).
- **NFR-0.2** Protocol conformance is a release gate: a protocol implementation that passes the conformance suite (NFR-10.2) must be able to run the canonical graph unchanged. This is how an adopter proves their runtime or VCS without kora's authors.
- **NFR-0.3** AI-first parity is a design constraint: every operation is exposed through the HTTP API with stable ids, idempotency keys and structured errors, so that MCP tools and skills (FR-0) are thin clients; no operation may depend on UI state or interactive prompts.

## 1. Scale

- **NFR-1.1 [target]** Organisation scale, calibrated on the owner's data platform: the existing Airflow-like ETL framework has ~300 graph authors per week, ~400 graphs and tens of thousands of scheduled processes; several dozen DWH teams of ~5 engineers each are the intended first users. kora must sustain **hundreds of agent nodes running concurrently**, hundreds of graphs, tens of thousands of runs per month, runs lasting hours to days (including human waits). MVP is validated at tens of concurrent nodes; the data model and scheduler must not need redesign to reach the target.
- **NFR-1.2 [MVP]** The control plane holds no in-memory run state; the scheduler is stateless over the metadata DB so that it can be scaled or replaced without losing runs. Disposable bounded external-call tasks carry no run authority; observation/delivery ownership and due times remain durable (ADR-0021). HA (multiple schedulers with leader election or partitioning) is a target, not an MVP deliverable, but the data model must allow it (row-level claims, heartbeats, lease expiry).
- **NFR-1.3 [MVP]** Waiting nodes (approval, sensor, timer) consume no runtime capacity: they are rows with a wake-up time or an event subscription, not held workers. A run without a non-terminal physical attempt occupies no execution-run slot, including retry/admission waits, while outstanding acceptance and an acquired target hold remain separate. Any live parallel or historical unresolved attempt retains capacity until closure (ADR-0020).
- **NFR-1.4 [target]** Fan-out of 100 instances in one run and nested fan-out inside sub-graphs are supported without special casing.

## 2. Reliability and durability

- **NFR-2.1 [MVP]** All run, node, attempt and artifact state is durable in the metadata DB before any side effect starts; the original runtime domain and every worker incarnation/lease-generation grant are persisted before dispatch authority returns. Every runtime submission is keyed by the persisted attempt id and deduplicated. A fresh claim can observe/adopt the same original-domain execution; stale API writes are rejected. Lease expiry is execution uncertainty, never termination proof. Replacement requires affirmative runtime quiescence and closure of every old dispatcher and unresolved platform publication; durable runtime retirement serializes with delayed submit/finalize and survives release (FR-6.1a, ADR-0017).
- **NFR-2.2 [MVP]** Every scheduler action is idempotent on (run id, node id, attempt id); triggers carry an idempotency key so that duplicate webhooks do not start duplicate runs.
- **NFR-2.3 [MVP]** Keep worker lease liveness and agent-loop liveness separate: lease expiry records unknown execution and enables recovery, while a stalled agent loop is detected and terminated by the supervisor within the configured interval. Neither heartbeat silence nor elapsed recovery time proves physical termination. A stalled failure is retryable by policy only after execution/dispatch/publication closure (ADR-0017).
- **NFR-2.4 [MVP]** Preemption/infrastructure failure of a runtime is distinguished from agent failure (separate error classes), so that confirmed infrastructure loss does not consume agent retry attempts. Unknown execution remains non-terminal with unchanged retry counts and reservations; no replacement starts until termination and dispatch/publication closure is established (ADR-0017).
- **NFR-2.5 [MVP]** Approval and sensor waits survive restarts and have an explicit timeout with a defined expiry behaviour; no wait is unbounded. Poll due times and retry backoff never extend that expiry. External calls hold no database transaction or run/scope lock; applying a result checks its exact current owner and token. Approval delivery intent survives crashes, with uncertain terminal cleanup retaining only compact identity/progress, no form/context payload or execution reservations (ADR-0021).

## 3. Performance

- **NFR-3.1 [MVP]** Scheduling latency from "dependencies satisfied" to "submitted to runtime" under 5 seconds at MVP scale (tens of concurrent nodes); runtime start-up latency (container start, checkout) is reported separately so it is attributable.
- **NFR-3.2 [MVP]** Session event streams are consumed incrementally (the normalised event stream is relayed as it is produced), not buffered until session end, so logs and cost counters are near-real-time and budgets can be enforced mid-session.
- **NFR-3.3 [MVP]** The IR for a graph with 1,000 nodes (after fan-out materialisation) validates and schedules without pathological cost; topological evaluation is incremental per state change, not a full-graph rescan.

## 4. Security

- **NFR-4.1 [MVP]** Secrets are references in IR, artifacts, logs and transcripts, namespace-bound and resolved through the secrets protocol. Sessions receive constructed minimal environments. Checkout credentials exist only during trusted preparation; publication credentials are acquired after immutable inspection and supplied through late finalization, including after recovery. Parent code-host credentials never enter candidate execution. Protected runtimes enforce control-state and publisher isolation; local is explicitly trusted development/testing without that hostile-code guarantee (ADR-0016). Captured output, errors and VCS results are redacted against resolved values.
- **NFR-4.2 [MVP]** Agent sessions run hermetically by default: the target repository's harness configuration (instruction files, hooks, MCP servers, settings) is not loaded unless `trust_repo_config` is set on the node config; the flag's resolved value, the node's permission mode and effective namespace/node execution policy are saved on the attempt and are auditable. Policy is sealed with the first nonsecret execution request after a current-revision check; new attempts use current policy, while recovery reuses its recorded values. Current authorization, secret checks, quota/budget admission and publication authority still apply; a namespace policy update does not promise live revocation of already sealed work (ADR-0018).
- **NFR-4.3 [MVP]** Persisted state is plain data (JSON); no pickled or otherwise executable payloads in the metadata DB, IR or approval responses.
- **NFR-4.4 [MVP]** Webhook triggers and the HTTP API require authentication (personal tokens bound to a person, a namespace list and the `approve`/`platform` flags; service principals for workers bound to a runtime and a namespace list); the frontend uses an opaque server-side session cookie with CSRF protection and a content security policy; the authentication/authorisation layer is a protocol so that OIDC/RBAC can replace it without changing the API.
- **NFR-4.5 [target]** Per-node network egress policy is a first-class field; runtimes declare whether they enforce it, and the compile step warns when a node requests a policy its runtime cannot honour.
- **NFR-4.6 [target]** Every attempt is attributable: harness name and version, resolved config digest, MCP server list, skills digest, approver identity, VCS refs produced. This forms the basis for later provenance attestations (in-toto shape).

- **NFR-4.7 [MVP]** A runtime declaring `protected_execution` prevents candidate control-state forgery, contains all descendants, seals publication input without writable aliases and isolates publication credentials. Namespace `require_protected_execution` rejects a runtime without this capability. Local declares false and requires no additional opt-in in a trusted installation; Docker and adopter runtimes must provide actual conformance evidence before claiming true (ADR-0016).

## 5. Extensibility and protocols

- **NFR-5.1 [MVP]** The following are protocols with at least one reference implementation and a conformance test suite: **runtime**, **harness adapter**, **VCS**, **approval channel**, **secrets provider**, **artifact storage**, **artifact type**, **observability exporter**, **authentication**, **sensor**, **policy**. The MCP server is not a protocol but a mandatory client surface of the API (FR-0). Control-plane external providers have a trusted cooperative lifecycle: bounded hooks/decoding, nonblocking I/O, propagated cancellation and declared serial dependency calls. MVP contains conforming slow-service failures, without hard isolation from blocking or cancellation-resistant installed Python; standalone shares that limit (ADR-0021).
- **NFR-5.2 [MVP]** Third-party implementations are discovered through Python entry points and configured by name in the IR; no kora source changes are needed to add one.
- **NFR-5.3 [MVP]** Harness versions are fixed per kora installation (images or installed binaries) and recorded on every `AgentSession` artifact; harness adapters declare the version range they were tested with, and an installation outside that range fails `kora doctor` and worker start, not individual nodes.
- **NFR-5.4 [target]** Chat approval channels, cloud-sandbox and Kubernetes runtimes, Vault secrets and RBAC are all implementable without changing the IR schema's major version.
- **NFR-5.5 [target]** Execution backend is replaceable: the scheduler's run-state model (runs, nodes, attempts, waits, artifact versions) is plain interpretable data and the scheduling loop's boundary is documented (as prose, not as a coded interface with one implementation), so a durable-execution engine (Temporal) can be introduced as an alternative backend without changing the IR, the artifact model or the protocols (see `knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md`).

## 6. Portability of the graph definition

- **NFR-6.1 [MVP]** The YAML IR is the canonical graph representation; it is versioned (`ir_version` root field for the format, `graph.version` for the author's graph version), has a published JSON Schema, and is designed for multi-language SDKs and UI editing: stable string ids, explicit layered defaults with documented precedence, graph separated from runtime and security configuration.
- **NFR-6.2 [MVP]** The Python SDK is a client of the IR, not its definition; any feature expressible in Python is expressible in the IR, and `kora compile` is deterministic (same source → byte-identical IR).
- **NFR-6.3 [target]** IR evolution follows semantic versioning with an in-place migrator (`kora migrate-ir`) for minor versions.
- **NFR-6.4 [MVP]** Three file classes are kept distinct in a project: human-authored source (Python, presets), machine-generated committed IR, and excluded secrets.

## 7. Operability

- **NFR-7.1 [MVP]** Single-command local start (`kora standalone`, SQLite) and single-command small deployment (`docker-compose up`: Postgres, an S3-compatible object store, scheduler, API, runtime). Helm chart is post-MVP.
- **NFR-7.2 [MVP]** Database migrations are versioned and run by `kora db upgrade`; upgrades do not require draining running runs: migrations within a minor version are additive only, indexes are built concurrently, scheduler and API tolerate the previous schema version, and the documented upgrade order is database, scheduler, API, workers. A compatible upgrade also retains the execution/type behavior needed by active contracts. Readers preserve saved historical results without invoking replacement plugins; unavailable required semantics cause an explicit incompatibility failure before affected execution effects, never silent reinterpretation. IR, record-format and execution-semantics compatibility are distinct from SQL compatibility (ADR-0018).
- **NFR-7.3 [MVP]** Every component exposes health and readiness endpoints and structured logs; the scheduler exposes basic metrics (queue depth, running attempts, scheduling latency) in Prometheus format. Existing reads/metrics also expose integration saturation, last successful observations, deadlines/backoff, provider degradation and pending/unknown approval delivery; external outages do not alone fail core database readiness (ADR-0021).
- **NFR-7.4 [MVP]** Runtime capacity is observable per runtime (running, queued, failed-to-submit, unknown execution count and oldest age, retained reservations/target holds and unresolved dispatcher/publication recovery) and limits are configurable without restart; outstanding accepted-run usage, distinct execution-run usage, effective cap source, target/recovery blockers and rejected trigger occurrences are visible through existing surfaces (ADR-0020). Workers register with the control plane and their liveness, the age of the oldest unclaimed attempt, webhook authentication failures, retention success and approval age are exported as metrics; a backup and restore procedure covering the database and the artifact store together is documented.

## 8. Cost governance

- **NFR-8.1 [MVP]** Token counts per model per attempt are recorded with a completeness state (`complete`, `partial`, `unavailable`) and, for resumed sessions, as a delta of whole-session totals; USD-equivalent is always labelled as an estimate with its basis (harness-reported, price table, gateway) and the billing class of the credential used. An execution observation gap exposes last-known figures with `observed_at` and at most `partial` completeness, never fabricated zero or complete cost; late usage remains historical under fresh recovery claims (ADR-0017).
- **NFR-8.2 [MVP]** Hard limits are enforced by kora regardless of harness: event counts from the normalized stream, inherited timeout policy from saved UTC deadlines and local monotonic timers. Managed phases share the earliest node/subgraph/run deadline; nominal interruption and forced-stop thresholds are `D` and `D + 30s`, under explicit clock/runtime-control assumptions, without an exact physical-stop guarantee (ADR-0022). Expiry never supplies termination evidence or releases capacity; late reports cannot restore current-result authority. Soft USD limits remain best-effort; overshoot headroom is a configurable percentage.
- **NFR-8.3 [target]** Cost-per-accepted-change (including failed attempts, retries and fan-out losers) is derivable from stored data without external systems.

## 9. Observability

- **NFR-9.1 [MVP]** Every run has a complete timeline: node state transitions, attempts, artifacts, approvals, cost, with run/node/attempt/session ids usable as join keys in external tools.
- **NFR-9.2 [MVP]** Observability export is opt-in and must not affect scheduling if the exporter is slow or down (asynchronous, bounded buffers, drop-with-metric).
- **NFR-9.3 [MVP]** Transcripts are stored as opaque harness output (version-tagged) plus a normalised event summary; kora never depends on parsing a harness's transcript file format to function.
- **NFR-9.4 [MVP]** Every executable component provides structured, leveled diagnostics from its first implementation, with INFO as the default and component-specific configuration. Authorized request/run/node/attempt/session context connects records where available; diagnostic identity grants no authority. Operational logs remain separate from durable events and session content, exclude sensitive payloads and secrets, and use bounded work/output with visible loss and bounded shutdown. Diagnostic-sink failure must not block scheduling, alter durable outcomes or delay execution-stop escalation. The [component logging contract](../../knowledge/specs/observability.md) defines levels, safe fields, context, failure behavior and the tests that activate with each component.

## 10. Testability and developer experience

- **NFR-10.1 [MVP]** A fake harness adapter and a fake runtime allow graphs to be tested end-to-end in CI without model calls; `kora plan` prints the materialised execution plan and `kora trigger --dry-run` executes a run on the fake runtime and harness. Behavioural scenarios (`kora test` and the stdio `kora_test` tool) supply agent outcomes by unambiguous selectors, execute command nodes for real with explicit reported overrides, inject approvals, events, time and randomness, enforce and report the absence of network and production secrets, and return machine-readable results, through the ordinary engine with configured fakes (dsl.md section 4).
- **NFR-10.2 [MVP]** Conformance suites exist for every protocol (NFR-5.1) so adopters can validate their enterprise runtime, VCS and secrets implementations.
- **NFR-10.3 [MVP]** Compile-time errors point to the Python source line of the offending node; IR validation errors point to the YAML path.

## 11. Compatibility

- **NFR-11.1 [MVP]** Python 3.11+; PostgreSQL 16+; SQLite for standalone; Docker Engine API as the Docker runtime dependency; Linux and macOS hosts for the local runtime.
- **NFR-11.2 [MVP]** Harness compatibility is declared per adapter (tested Claude Code and Codex version ranges) and checked at installation health check and worker start, not per node.
- **NFR-11.3 [target]** The control plane API is versioned (`/api/v1`) and backwards compatible within a major version.

## 12. Licensing and governance

- **NFR-12.1** Apache-2.0 license; all protocols and reference implementations are in the open-source core. No "enterprise-only" protocol.
- **NFR-12.2** Documentation language is English; the IR schema, protocol interfaces and CLI are documented as public contracts with changelogs.

## 13. User experience

- **NFR-13.1 [MVP]** The web console is reference-grade in user experience, not merely present. It covers what an operator of a mature orchestrator such as Airflow expects (runs, a graph view with live node states, attempts, logs, artifacts, cost, approvals) with the lightness of Apple's product pages and Anthropic's apps: one primary task per screen, generous whitespace, restrained typography and colour, progressive disclosure of detail, no dense control panels, fast and calm interactions. A view that is correct but cluttered does not meet this requirement; every view passes a design review against this bar before release.
