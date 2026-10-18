# Design decision identifier index

Historical identifiers from the owner's October 15, 2026 decisions. The original record is preserved at [the consolidation baseline](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/reviews/design-decisions-2026-10-15.md). This compact index records current owners and supersession; it is not a second specification. Read the linked ADR and component section. An accepted design is not evidence of an implemented or tested feature. DD-10 was never assigned; identifiers are not renumbered.

## Decisions and current owners

| Identifier | Disposition and current owner |
|---|---|
| DD-1 | Seven trigger rules; [IR nodes](../specs/ir.md#3-nodes) and schema `triggerRule`. |
| DD-2 | Sensor provider; [protocols section 11](../specs/protocols.md#11-sensor-fr-23-fr-62-dd-2), under [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md). |
| DD-3 | CEL strings and schema-driven checking; [IR expressions](../specs/ir.md#7-expressions-and-templates), [ADR-0013](../../docs/decisions/0013-implementation-stack.md). |
| DD-4 | Retention; [data-model section 10](../specs/data-model.md#10-retention-dd-4). The original 90/180-day values are superseded by [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md); payload availability also follows ADR-0018. |
| DD-5, DD-16 | Agent nodes never operate kora; no per-attempt operator tokens, recursive child-run controls or child-cost attribution. [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md). |
| DD-6 | No per-token rate limits in MVP; [API limits](../specs/api-and-agent-surface.md#8-rate-and-size-limits). Bounded start acceptance is separately required by ADR-0020. |
| DD-7 | Own scheduler; deferred alternative backend with a prose boundary. [ADR-0001](../../docs/decisions/0001-own-control-plane.md), [scheduler section 12](../specs/scheduler.md#12-execution-backend-boundary-nfr-55). |
| DD-8 | No transform node. Original model-based packing/selection is superseded by deterministic commands in the canonical graph; narrative remains model-authored. [ADR-0012](../../docs/decisions/0012-no-transform-node.md). |
| DD-9 | Fan-out bodies are subgraphs; inputs feed bodies and fan-out collects outputs. [IR composition](../specs/ir.md#4-node-kinds-and-their-config), [ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md). |
| DD-11 | Full sandboxed Jinja2 retained; [IR expressions](../specs/ir.md#7-expressions-and-templates). |
| DD-12 | Overlap policy; original universal `allow` default superseded for webhooks. [IR triggers](../specs/ir.md#8-triggers-fr-62), ADR-0006/0020. |
| DD-13 | Outbound workers use HTTP only; [ADR-0003](../../docs/decisions/0003-scheduler-and-workers-over-http.md). Recovery and deadlines are amended by ADR-0017/0022. |
| DD-14 | Installation-level harness versions, tested range and visible resume fallback; [ADR-0004](../../docs/decisions/0004-harness-adapters-over-vendor-sdks.md). |
| DD-15 | Stall default and overrides; [IR limits](../specs/ir.md#9-limits-fr-65-fr-66-fr-94) and scheduler section 4. Inherited deadlines cap extension under ADR-0022. |
| DD-17 | HMAC or replay-protected bearer webhook authentication; [IR triggers](../specs/ir.md#8-triggers-fr-62). |
| DD-18 | Stdio and HTTP MCP forms; [API MCP section](../specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28). |
| DD-19 | Server-side browser session and fallback token login; one-time links added by [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md). Cookie/Bearer bridging remains an open question. |
| DD-20 | Native skill mappings; [harness mapping](../specs/protocols.md#14-harness-mapping-tables-fr-33-dd-27). |
| DD-21 | Runtime network enforcement with harness restrictions as an additional layer; [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md). |
| DD-22 | Workspace-kind matching superseded by typed plans and shared executors; [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md), amended by ADR-0016. |
| DD-23, DD-30 | Environment-injected harness credentials in reference MVP runtimes; per-attempt credential proxy deferred. [Runtime protocol](../specs/protocols.md#2-runtime-fr-81), ADR-0004/0009. |
| DD-24 | Keyword-only node inputs; [DSL](../specs/dsl.md), ADR-0006. |
| DD-25 | Local and registry presets, pulled separately and snapshotted at compile time; [DSL](../specs/dsl.md), ADR-0006. |
| DD-26 | Optional graph repository default; every workspace-using node still needs a source. [DSL validation](../specs/dsl.md), ADR-0006. |
| DD-27 | Vendor interfaces retained; old Codex SDK and MVP ACP wording superseded by direct app-server and post-MVP ACP. [ADR-0004](../../docs/decisions/0004-harness-adapters-over-vendor-sdks.md). |
| DD-28 | Curated task-shaped MCP tools and operation/schema parity; old count and skills-as-MCP-resources wording superseded. Current inventory: [API sections 4–5](../specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28). |
| DD-29 | One `tool_policy`, namespace deny precedence and explicit hook coverage; a permission prompt is not a universal interception hook. [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md). |
| DD-31 | Namespace daily soft limit blocks admission; [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md), scheduler section 4. |
| DD-32 | Personal tokens, two flags, transcript capability and preset escalation refusal; [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md), [DSL](../specs/dsl.md). |
| DD-33 | Platform facts separate from agent narrative; [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md). |
| DD-34 | Protected gate paths and explicit measurement provenance, not evaluator attestation; [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md). |
| DD-35 | Reviewer identity, allowlist, opt-in merge signal and filtered feedback; [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md). |
| DD-36 | Implementation defaults owned by the component sections below; later ADRs override original mechanisms. |
| DD-37 | Small-mechanism dispositions and reasons retained below, with current owners. |

## DD-36 implementation-default ownership

| Default | Current contract owner |
|---|---|
| Standalone bootstrap, SQLite settings and generated token | Scheduler section 10 |
| Self-contained claims, leases, polling and recovery | Scheduler section 3; API section 2; ADR-0017 |
| Agent output cardinality and AgentSession fields | IR section 3 |
| Repository resolution, Git credentials and publication | Protocols section 4; ADR-0016/0017/0022 |
| Harness credentials and normalized event shape/counting | Protocols section 3 |
| Termination/grace | Protocols section 2; scheduler section 17; ADR-0022 |
| Claude client settings, structured-output fallback and usage mapping | Protocols sections 3 and 14 |
| Finalization ownership and cancelled instances | Data-model section 4; scheduler section 2 |
| Trigger idempotency and parameter validation | API section 1; data-model section 13; ADR-0020 |
| Artifact upload, full payload/approval digests, inline threshold and paths | Data-model section 3; protocols section 7; ADR-0018/0019 |
| Command outputs | IR section 4 |
| Approval response and API body shapes | API section 2 |
| Compile conventions | DSL sections 1–2; ADR-0002/0006 |
| Tick semantics, fixed-point cap and discovery | Scheduler section 8; ADR-0003 |
| Event kinds and payload ownership | Data-model section 2 and component transition sections |
| CLI run/list columns | API section 3 |

These are links by document and section to the [scheduler](../specs/scheduler.md), [API](../specs/api-and-agent-surface.md), [IR](../specs/ir.md), [protocols](../specs/protocols.md), [data model](../specs/data-model.md) and [DSL](../specs/dsl.md). No remaining default is defined only in the historical body.

## DD-37 retained and removed mechanisms

| Disposition | Mechanism, reason and current owner |
|---|---|
| Retained | Schedule pause/resume stops cron without redeployment; API sections 2 and 4 (`kora_admin`). |
| Retained | `success_exit_codes` permits tools whose nonzero result denotes a difference; IR command config. |
| Retained | `reasoning_tokens` preserves separately reported usage for price attribution; data-model usage and protocols harness mapping. |
| Retained | Sensor `min_interval` protects code hosts against tight polling; protocols section 11. |
| Removed from v1 | `timer.until`: duration or absolute `at` already expresses timer wakes; IR section 4. |
| Deferred | Cron catch-up/backfill; v1 fires forward only, IR section 8. |
| Removed from v1 | Storage presign operation; API-proxied downloads remain the single path, protocols section 7. |
| Removed | Sensor cursor; sensors are stateless between polls, protocols section 11. |
| Retained | Commands can emit a declared custom artifact type from `output_file`; IR section 4 and worked fixtures. |
