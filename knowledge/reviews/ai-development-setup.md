# AI development verification

This development workflow was designed under [issue #87](https://github.com/realkarych/kora/issues/87). Its scope is contributor verification in Codex and Claude Code, not kora's product harness skill packages. [AGENTS.md](../../AGENTS.md) requires the checkpoints; [kora-verify](../../.claude/skills/kora-verify/SKILL.md) owns routing and evidence. Product behavior remains in the specifications and their cited decisions.

## Minimal inventory

There is one canonical skill in `.claude/skills/kora-verify`, exposed to Codex by the relative directory symlink `.agents/skills/kora-verify`. `CLAUDE.md` imports `AGENTS.md`. Four reference profiles define distinct reviews: correctness, browser behavior/diagnostics, controlled performance measurement and security boundaries. In the development workflow, each selected profile runs in a fresh generic child; the normal parent coordinates and implements.

Correctness spans the component contracts, so separate permanent architect, API, database, documentation and test-writing agents would duplicate ownership. Browser and performance remain separate reviews because performance needs an uncontended measurement environment and an explicit conclusion beyond successful interactions. Security owns threat-based reasoning beyond correctness and scanner output. References avoid duplicated host-specific agent definitions. No custom router, report validator, extra command skill or global plugin configuration is required.

Independent plan review checked minimality, invocation and component coverage. The accepted corrections made scope semantic as well as path-based, included renamed/deleted/untracked files, protected existing obligations against policy edits, separated design from executable evidence, and included installed-provider and product-skill coverage. Actual host acceptance and limitations are recorded in [dated host evidence](../research/ai-development-host-support-2026-10-18.md).

## GitHub review entry point

Root `AGENTS.md` has `Code Review Rules` directing native Codex Review to the same skill's GitHub reviewer mode. It applies the relevant profile criteria in one read-only pass; it does not launch the implementation coordinator or claim several independent reviews. The normal coordinator's specialist and executable gates remain required.

In a connected repository, a PR comment containing `@codex review` requests a native review without naming the skill. Automatic reviews additionally require the repository's Automatic review setting in [Codex review settings](https://chatgpt.com/settings/code-review); repository files alone do not enable that setting. A GitHub label is not configured as a trigger. No extra skill, bot, Action, credential or global local-agent setting is added. See the dated host record for observed activation and limits.

## Component checklist

Use [the knowledge index](../README.md) for exact contracts and ADRs. This checklist assigns review focus; it does not replace specifications or prescribe future source paths. Every row receives correctness review; the skill selects specialists from the effects of the change. Add new implementation areas when their first PR lands.

| Component | Check focus |
|---|---|
| DSL/compiler/presets | Determinism, precedence, source locations, client-side Python execution and IR/schema agreement |
| IR/CEL/Jinja/graph validation | Types, cycles, references, bounded evaluation, fan-out/subgraphs and hostile input |
| Metadata/state/lineage/retention/cost | Both databases, immutable history, result authority, usage completeness/deltas, retention and arithmetic |
| Scheduler/admission/triggers/waits/retries | Idempotency, locks, capacity/target holds, stale observations, deadlines, recovery and provider-stall fairness |
| API/shared Python client/CLI/MCP/generated TypeScript | Operation/schema parity, structured errors, auth, idempotency, pagination, streaming and noninteractive use |
| Product harness skills/shared references | Generation drift, every operation/example and error/recovery hint; installation in user sessions |
| Worker/supervisor | Epoch fencing, original-domain adoption, checkpoints, journal authority, descendant shutdown and disconnected operation |
| Runtime protocol/local/Docker | Shared conformance, lifecycle, deadlines, idempotency/recovery and actual protected-runtime evidence |
| Harness adapter protocol/fake/Claude/Codex | Recorded SDK streams, normalization, usage/liveness, version compatibility, hermetic configuration and policy interception |
| VCS protocol/Git/other providers | Unchanged canonical graph, inspected revision/destination, history, response-loss reconciliation and no unintended overwrite |
| Approval-channel protocol | Bound content, invalidation, permissions, expiry, durable delivery and uncertain cleanup |
| Secrets protocol | Namespace rights, minimal environments, redaction, phase-limited credentials and no stored resolved values |
| Artifact-storage protocol | Digests, integrity/size bounds, upload/download, retention/failures and local/S3-compatible behavior |
| Artifact-type protocol | Schema closure, saved semantics, validation/summaries, compatibility and malicious payloads |
| Observability-exporter protocol | Correlation, bounded buffering, outage independence, drop metrics and redaction |
| Authentication protocol | Namespace/object access, user/service separation, approve/platform/transcripts, cookies/CSRF and tokens |
| Sensor protocol | Duplicate events, bounded polling, expiry/cancellation, stale ownership and outage isolation |
| Policy protocol | Deny precedence, argv indirection, declared tool coverage, protected paths and capability refusal |
| Provider registry/configuration/distribution | Installed local third-party fixture, `kora.provider` discovery/metadata/configuration, plugin listing and external conformance without core imports |
| Console | Changed journeys, two pilots, four mutations, unknown/partial/stale states, reconnect/login, keyboard/accessibility, responsive design and tokens |
| Packaging/deployment/migrations | Wheel/web assets, install/start/doctor, deployment/standalone, active-run upgrades, health/readiness and backup/restore |
| Development configuration/CI/skills | Discovery/dispatch, policy consistency, permissions, pinned tools, secret-free evidence and preserved checks |

## Activation and completion

### Component diagnostics

[Structured component diagnostics](../specs/observability.md) applies across every component row above, beyond the observability-exporter protocol. Its [activation matrix](../specs/observability.md#7-component-coverage-and-activation) names component and all eleven provider obligations; its [future evidence table](../specs/observability.md#8-future-executable-evidence) defines named integration/conformance scenarios. Issue #91 owns pending inventory until each implementation issue assumes its rows before code starts. The first real backend consumer ships shared logging infrastructure and tests; subsequent component PRs include their applicable diagnostics and evidence. The existing shell's browser/performance obligations remain active.

Correctness reviews levels, event ownership, context and stream separation for each changed component. Security reviews content, identity and channel boundaries; performance reviews bounded work, noise, loss and sink independence. Changes to collection, redaction, propagation, buffers or sinks select all three profiles at planning and readiness, including contract-only changes. This extends the same router and native GitHub lenses, without a fifth reviewer or replacement for executable checks.

| Example scope | Observability selection and evidence |
|---|---|
| Scheduler behavior | Correctness checks events/levels/context; performance and any other matching profiles remain required; real integration covers volume, durable events and sink independence when implemented |
| Redactor or sink change | Correctness, security and performance before plan/readiness; content, lifetime/cost, saturation and shutdown evidence |
| CLI output | Correctness checks exact stdout and request IDs; existing client/backend semantics select other applicable profiles; changed collection/sinks select all three |
| Provider-only logging | Provider name/path is no exemption: bound-context/output changes select all three; conformance and real core integration provide evidence |
| Editorial documentation only | Record the concrete applicability reason; no invented runtime logging tests; every PR still receives correctness and security applicability assessment |
| Logging deletion or rename | Inspect removed behavior and semantic effects using stricter base obligations; required coverage cannot disappear with the filename |

### Existing activation rules

At setup, the repository contains specifications, examples and documentation/schema CI, not an executable application. This workflow does not claim product lint, type checks, tests, browser behavior, security scans or performance measurements have passed. Unimplemented capabilities are `pending implementation`; an existing capability that cannot be checked is `blocked`.

- The first executable backend component activates relevant integration/conformance and performance checks even without a UI. Preserve strict static checks, both databases, supported platforms and deterministic tests under `AGENTS.md`.
- A frontend package-only scaffold activates build/static checks. The first runnable shell activates Playwright boot/diagnostics/accessibility and loading-performance checks. Every subsequent frontend/backend change runs common smoke plus affected journeys and separate applicable performance checks.
- Each view/action adds its journey and negative-case coverage. When implementation starts, record executable versus pending segments in the existing pilot-scenario document; both complete pilots are required before console/MVP completion.
- Each provider feature adds its conformance gate. Protection claims require real protection tests; supported harness versions additionally need documented manual live compatibility probes outside deterministic product CI.
- Each activation PR registers actual local commands and CI jobs, evidence, budgets where applicable, and required merge statuses. Include any owner/admin configuration gap. Missing tools are never silently installed or counted as passing; dependency adoption follows repository policy.

Each implementation issue owns its pending activations and concrete trigger, rather than a generic future TODO. Existing required checks cannot be downgraded because a feature was renamed or removed. Open architecture proposals and issues, including #41–#46 and #71, are not accepted contract overrides.

Prompt routing is procedural, not deterministic enforcement. Representative observed host calls demonstrate compatibility only under their recorded conditions. Reviewers inspect source and evidence; a report does not prove that a child ran. Required CI statuses enforce executable checks, with all mandatory jobs passing on the final PR revision and the whole pipeline remaining below ten minutes. Long optional exploration can supplement, but not replace, required coverage.
