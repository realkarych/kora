# ADR-0004: Harness adapters drive harnesses through vendor interfaces, not CLI output; harness versions are fixed per installation; kora enforces the limits

- Status: accepted, 2026-10-15; amended 2026-10-16 (the ACP adapter moved after MVP, requirements decision 22)
- Reviewed: by the project owner, 2026-10-16
- Deciders: project owner, after review of the first design drafts (DD-14, DD-20, DD-23) and after the design review of 2026-10-15 (first cycle: DD-27, DD-30)
- Absorbs: DD-27, DD-14, DD-20, DD-23, DD-30; requirements decision 11; the harness part of requirements decisions 18 and 19
- Related: [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0009](0009-target-repository-trust-boundary.md), [ADR-0010](0010-cost-accounting-and-limits.md)
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

## Context

An agent node runs one full coding-agent session on a harness, Claude Code or Codex in MVP. kora must start the session with the resolved node configuration, stop it when a limit is reached and record what it cost. Requirements decision 18 first ran harnesses as CLI processes and parsed their JSONL output. The [design review of 2026-10-15](../../knowledge/reviews/review-2026-10-15.md#harness-adapters-over-vendor-sdks) contested this because both vendors publish SDKs with a message model and hook points, and found the [credential proxy unimplemented](../../knowledge/reviews/review-2026-10-15.md#credential-proxy-deferred) for the MVP runtimes. A per-node harness version pin had already been dropped as meaningless when binaries ship in an installation image (DD-14).

## Decision

**Two built-in adapters in MVP, no CLI JSONL parsing.** The `claude` adapter uses the Claude Agent SDK for Python, which spawns the pinned `claude` binary and speaks to it over stdio. The `codex` adapter speaks the `codex app-server` JSON-RPC protocol directly through a small kora client, because the Codex SDK exists only for TypeScript (aligned on 2026-10-16). After MVP an `acp` adapter speaks the Agent Client Protocol (JSON-RPC over stdio) to OpenCode, Pi, Gemini CLI and other ACP-capable agents (`harness: acp:<agent>`); it is added through the same harness protocol and conformance suite, without a change to the graph format.

**The session runner is the single enforcer.** Every adapter runs inside `kora-runner`, a kora entry point present in every runtime image or host install. It forwards each SDK message as a normalised event and alone enforces `max_events`, the soft USD budget and `stall_after` from that stream, regardless of harness. Native limits (Claude Agent SDK `max_turns` and `max_budget_usd`, the Codex token budget when enabled) are set in addition, never relied on. Hence requirements decision 11 replaced the early `max_turns` node limit with `max_events`: a turn limit would be promised on behalf of every harness, whereas `max_events` (turns, tool calls, tool results) is counted by kora and terminated identically everywhere (interrupt, 30 s grace, kill, error class `events_exhausted`).

**Usage completeness is explicit**: `complete` (final result received), `partial` (summed from per-turn events after a crash or kill; Codex reports usage only when a turn completes, so a killed turn is unaccounted) or `unavailable`. For `resume` retries the usage is the delta of whole-session totals.

**Harness versions are fixed per installation.** A node has no `harness_version` field. The installed version comes from the SDK's init message, is recorded on the `AgentSession` artifact and the attempt, and is never compared with a node requirement. Each adapter declares `tested_versions`; an installation outside it fails `kora doctor` and worker start, not individual nodes. Images ship with the harness auto-updater disabled. A `resume` retry whose installed version differs from the previous attempt's falls back to `fresh_with_feedback` and records `strategy_fallback`.

**Codex skills are native** through `skills.config[]` in `config.toml`, so `skills` maps natively on both vendor harnesses; ACP agents, once supported, get a warning unless they read skills from the workspace.

**Credentials are environment variables in MVP**, taken from the adapter's deployment configuration as secret references resolved at submit and placed in the constructed minimal environment. The runtime capability `credential_proxy` stays in the protocol; the reference `local` and `docker` runtimes declare it false, and the per-attempt proxy is the first post-MVP runtime item.

## Alternatives considered

- **CLI processes with JSONL streams** (requirements decision 18). Replaced by DD-27: the output format is not a contract, hooks and structured output are reachable through the SDKs, and the Codex app-server needs a JSON-RPC client anyway.
- **Relying on harness-native limits.** Rejected: they differ per harness, so a node's behaviour would depend on the adapter running it.
- **A per-node `harness_version` pin.** Rejected by DD-14: binaries live in the image, so a node cannot choose one.
- **Enforcement split between worker and runner.** Rejected by the [design review of 2026-10-15](../../knowledge/reviews/review-2026-10-15.md#one-enforcement-point).
- **Credential proxy in MVP.** Deferred by DD-30; the capability flag keeps the protocol stable.
- **The ACP adapter in MVP.** Deferred on 2026-10-16 (requirements decision 22): the MVP acceptance test and the canonical graph need only Claude Code and Codex, and the harness protocol lets the adapter arrive later without breaking changes.

## Consequences

- Adapters must be importable inside runtime images; harness binaries and `kora-runner` belong to the runtime installation, not to the graph.
- Adding a harness means implementing one protocol and passing its conformance suite (recorded SDK streams, `max_events` termination at the exact count, hermetic behaviour, the policy hook reached for every call kind the adapter declares covered, version checked). A harness upgrade is an installation change; running attempts keep the version they ran with.
- Soft USD limits remain best-effort (NFR-8.2); until the proxy ships, a session can read its API key from its environment, with network policy and tool policy as compensating controls.

## Where it is applied

- [FR-2.1, FR-3.1, FR-3.3, FR-6.4, FR-6.6, FR-8.8, FR-9.1, FR-12.2](../requirements/functional.md)
- [NFR-5.3, NFR-8.1, NFR-8.2, NFR-11.2](../requirements/non-functional.md)
- [protocols.md §2 (session runner, `credential_proxy`), §3, §13, §14](../../knowledge/specs/protocols.md)
- [worker components](../../knowledge/architecture/detailed.md#components-inside-a-worker), [protected execution boundary](../../knowledge/architecture/detailed.md#protected-execution-boundary), [stack](../../knowledge/architecture/detailed.md#stack-per-component) and [run walkthrough](../technical-introduction.md#4-one-run-through-the-core)
- [ir.md §4.1, §9](../../knowledge/specs/ir.md)
- [scheduler.md §7](../../knowledge/specs/scheduler.md)
- [data-model.md `attempt`, §7](../../knowledge/specs/data-model.md)
- [DD-14, DD-20, DD-23, DD-27, DD-30](../../knowledge/reviews/design-decisions-2026-10-15.md)
