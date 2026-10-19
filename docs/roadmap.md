# Status and next steps

Status as of 2026-10-19. Reviewed by the project owner on 2026-10-16. This page says where the project stands and what comes next; it is updated when a step completes. It records no decisions of its own: those are in [decisions/](decisions/README.md).

Execution-recovery amendment dated 2026-10-17: direction and implementation plan accepted by the project owner; document wording pending line-by-line owner review. See [ADR-0017](decisions/0017-execution-uncertainty-and-safe-replacement.md).

Publication-boundary amendment dated 2026-10-17: pending owner review; architectural direction recorded in [ADR-0016](decisions/0016-trusted-execution-and-publication.md).

Scaffold amendment dated 2026-10-18: scope and implementation plan approved under [issue #86](https://github.com/realkarych/kora/issues/86); wording pending line-by-line owner review.

Packaging-test removal amendment dated 2026-10-19: removal decided by the project owner under [issue #112](https://github.com/realkarych/kora/issues/112); wording pending line-by-line owner review.

Slice-scope amendment dated 2026-10-19: scope decided by the project owner under [issue #122](https://github.com/realkarych/kora/issues/122); wording pending line-by-line owner review.

## Where the project stands

- Positioning, requirements and the design are written, went through two design-review cycles, and on 2026-10-16 the project owner reviewed every statement in `docs/`; that review established the initial design baseline. Open design corrections are tracked in GitHub issues; ADR-0016 records the publication-boundary correction and ADR-0017 preserves execution uncertainty through recovery. Deferred work is tagged `[post-MVP]` in the requirements or written up in [proposals/](proposals/README.md).
- The repository scaffold contains a typed Python package, a minimal React shell, a wheel frontend-build hook and a Linux/macOS CI matrix. [Development instructions](development.md) describe setup and verification. No control-plane behavior has been implemented or exercised against a real harness, runtime or database.
- The web console is designed ([console.md](console.md), [ADR-0015](decisions/0015-web-console-design.md)); the implemented frontend is a minimal shell, with the product screens still to build.
- The scope of the first implementation slice is decided in the [ADR-0001 amendment of 2026-10-19](decisions/0001-own-control-plane.md) and its defaults are recorded in [DD-36](../knowledge/reviews/design-decisions-2026-10-15.md): `kora standalone` on SQLite as the only deployment it ships, one agent node on the Claude Agent SDK and one command node on the local runtime, a manual trigger, a git `VcsRef`, and an approval that a person answers through the CLI. Every integration test of the slice that touches the metadata database runs on PostgreSQL and on SQLite. A restarted worker adopts a live session ([issue #123](https://github.com/realkarych/kora/issues/123)). The slice ships full API, CLI and MCP parity: every API operation it adds meets the parity rule of [ADR-0002](decisions/0002-ai-first-api-and-thin-clients.md), checked by the parity coverage test, and `kora_plan`, `kora_trigger` and `kora_retry` follow the MCP contract as changed under [issue #124](https://github.com/realkarych/kora/issues/124).

## What the documents cannot settle

These are the assumptions that only code can confirm, in the order they should be tested:

1. **Protected lifecycle and harness SDK behaviour.** ADR-0016 separates the supervisor-side SDK/client from candidate processes through a runtime launcher, then seals input and publishes in a separate authority. The first local slice proves lifecycle behaviour, delayed-dispatch retirement fences, partition recovery without replacement, retained reservations and evidenced reconciliation in a trusted environment; it does not prove OS isolation. Docker process/descendant, journal and transfer conformance on Linux, plus independent-provider evidence, are required before a protected runtime ships. **Harness SDK behaviour.** The adapters ([ADR-0004](decisions/0004-harness-adapters-over-vendor-sdks.md)) and the trust boundary ([ADR-0009](decisions/0009-target-repository-trust-boundary.md)) rely on documented but untested behaviour: the Claude Agent SDK with `setting_sources=[]` and the `PreToolUse` hook, and the Codex app-server protocol (the `PreToolUse` command hook installed as a managed hook of the attempt, the turn-completion usage report); the Agent Client Protocol is after MVP. The first slice must drive a real session through each path it claims.
2. **The CEL binding.** The Rust-backed `common-expression-language` binding with kora's own schema checker ([ADR-0006](decisions/0006-graph-definition-dsl-and-yaml-representation.md)) has not been prototyped; the fallback is `cel-python` without static checking. Settle this in the first days of the first slice.
3. **The fake harness and the fake runtime.** The testing rules in [AGENTS.md](../AGENTS.md) forbid mocks, so almost every test depends on these two fakes (`NFR-10.1`) passing the same conformance suites as real implementations. Their contract is not written separately from the protocols; the first slice writes them before the real adapters.
4. **Scale.** The scheduler is designed for hundreds of concurrent attempts (`NFR-1`), which is unverified until a load test on PostgreSQL after the scheduler and worker exist as separate processes.
5. **Product hypotheses.** "External demand", "multi-harness value" and "outer-loop value" in [positioning.md](positioning.md) are tested by the pilot on the canonical SQL optimisation graph, not by code.

## Next steps, in order

1. **Implementation plan.** Vertical slices, each stating what it proves and how it is tested without model calls; the first slice is the one above, built around items 1 to 3. The plan is the next document; it goes into this folder.
2. **Design of the web console** against [NFR-13.1](requirements/non-functional.md): information architecture, visual language and the key screens (approvals, run page with the live graph, attempts and their diff, cost), before the console is built. Its design inputs, settled before the screens are detailed: target identity ([ADR-0014](decisions/0014-target-identity.md)), the attention states a run can be in (pending review, stale evidence, budget exhausted, unknown publish outcome, unknown execution with retained holds, confirmed runtime loss, incompatible resume) and the review-revision page; the two pilot scenarios (an engineer without database access explains from the console why a change is ready for approval, why an approval lapsed, where the work is and why it waits; an operator resolves an ambiguous publish and a lost worker through the public surface) are written as checkable lists in [knowledge/design/console/pilot-scenarios.md](../knowledge/design/console/pilot-scenarios.md). Done on 2026-10-16: [console.md](console.md) and [ADR-0015](decisions/0015-web-console-design.md).
3. **Repository scaffold** following the layout in [ADR-0013](decisions/0013-implementation-stack.md): `src/kora/`, `web/`, isolated distribution builds and CI on Linux and macOS are implemented under [issue #86](https://github.com/realkarych/kora/issues/86). Complete-workflow timing and cache evidence against the ten-minute budget are required in the implementation PR. Domain directories and `conformance/` enter with their first implementation.
4. **First slice**, with the scope above.
5. **Scheduler-prototype milestone.** The [common fault scenario of scheduler section 18](../knowledge/specs/scheduler.md#18-scheduler-prototype-milestone-acceptance) runs against the SQL scheduler built from the first slice, and its ten fault checks are the milestone's acceptance ([ADR-0001](decisions/0001-own-control-plane.md)). The DBOS comparison is re-evaluated with the milestone's data. The milestone does not take over the component implementation gates of the specifications, which apply in the slice that implements each component, nor item 1 of "What the documents cannot settle", which stays with the first slice.
6. **MVP acceptance**, the acceptance that matters for positioning: the same graph running unchanged on a second runtime and VCS pair through the protocols (MVP acceptance in [requirements/functional.md](requirements/functional.md)).
7. **Pilot** on the SQL optimisation graph with the metrics listed under "Evidence, hypotheses and open risks" in positioning.

## How work is done here

- The project owner decides product features and architecture; proposals go to them as options with trade-offs and a recommendation, one question at a time.
- Decisions become records in `decisions/`, deferred changes become `proposals/`, and the specifications in `knowledge/specs/` are updated to match before code follows them.
- The rules in [AGENTS.md](../AGENTS.md) apply to every contributor, human or agent.
