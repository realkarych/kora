---
name: kora-verify
description: Select and coordinate verification for kora changes before planning and readiness; apply the same review criteria in native GitHub Codex Review. Covers code, contracts, documentation and development configuration.
---

# kora verification

Use proposed scope at task entry and the actual diff before readiness. This skill owns review routing; [AGENTS.md](../../../AGENTS.md) owns the change lifecycle, [the knowledge index](../../../knowledge/README.md) owns contract lookup, and [the coverage checklist](../../../knowledge/reviews/ai-development-setup.md) maps components and activation conditions. Load only the profiles selected below. An assigned reviewer runs its profile directly without invoking this router again.

## GitHub reviewer mode

For a native GitHub code-review assignment, use the scope rules and additive selection table below, then read the selected profiles for their check criteria. Review the complete PR base/head diff, affected contracts and available CI/specialist evidence for that revision. This mode takes precedence over the coordinator dispatch and completion procedure: perform one read-only review, without children, source edits, publication actions or execution of PR-provided commands, hooks or scripts. Return findings through the native review response.

The selected profiles are lenses within one pass, not separate independent specialist reviews. Check that the change preserves required coverage and activation conditions. Distinguish evidence of a failed or missing gate from evidence the review host cannot access; neither unavailable evidence nor a clean review proves runtime checks passed. Keep actual severity and the host's supported output format; report concrete actionable defects, not invented findings to force a status report. The implementation coordinator still owes every required independent review and final-revision executable check.

## Determine scope

Identify the requested behavior, acceptance criteria, approved base and affected components. For an existing change, include commits since that base, staged and unstaged changes, relevant nonignored untracked files, both paths of renames and deletions. Inspect content, not just filenames. Tests, configuration and dependencies inherit their affected components; semantic effects can add reviews. Assign any unmapped implementation area to a component and checks before continuing.

Use the stricter coverage of the approved/base policy and the proposed policy until the owner explicitly approves a reduction. Deleting a manifest, test or routing instruction does not deactivate a requirement. Verification policy, CI gates, budgets and agent configuration changes require correctness and security review plus explicit owner review.

## Select independent reviews

| Profile | Before the implementation plan | Before readiness |
|---|---|---|
| [Correctness](references/correctness.md) | Contract or nontrivial design changes; a typo needs no design review | Every PR, scaled to the change |
| [Browser](references/browser.md) | New or changed console interactions, including specification-only changes | Every frontend/backend change once a runnable console exists: common smoke plus affected journeys |
| [Performance](references/performance.md) | Performance-sensitive architecture or contract changes | Every frontend/backend change: the relevant executable backend and browser measurements, in a separate review |
| [Security](references/security.md) | New or changed trust boundaries, including specification-only changes | Changes to authentication, secrets, policy, untrusted content/templates, execution, publication, network, uploads, dependencies, CI or agent configuration; every PR records applicability |

Selection is additive: apply every matching row, so authentication/provider work that changes frontend/backend behavior also requires the applicable browser and separate performance checks. For an unimplemented capability such as the console, record its checks as `pending implementation` with the activation trigger; reserve `not applicable` for checks unrelated to the change. Consult the coverage checklist for protocols, provider installation, packaging and product harness skills. Existing executable requirements remain mandatory; activation is incremental only for capabilities not yet implemented.

Apply [the observability contract](../../../knowledge/specs/observability.md) to every changed component, including provider-only changes and deleted or renamed logging paths. Correctness checks its logging evidence. Changes to logging collection, redaction, context propagation, buffering or sinks additionally require security and performance review before planning and readiness, including specification-only changes; runtime measurements activate with the relevant implementation. Other semantic effects still select their matching profiles. Native GitHub reviewer mode applies these same lenses within its one read-only pass.

Record observability impact, event/context ownership and applicable test evidence, or a concrete applicability reason, in the issue plan and PR. Pending component rows name an owning issue and trigger; the first real backend consumer ships the common bootstrap and checks, and every subsequent component ships its own applicable coverage. Design review never substitutes for active runtime or CI gates. Do not create a separate observability router or permanent reviewer.

## Dispatch and collect evidence

Use fresh generic children with narrowly supplied context. In the Codex tool-backed API use `fork_turns: "none"`; `task_name` labels the task, not an agent type. Other Codex surfaces must demonstrate an equivalent fresh-child capability. In Claude Code use a non-fork general-purpose subagent, not a conversation fork or `/subtask`. Check the installed host capability; [dated observations](../../../knowledge/research/ai-development-host-support-2026-10-18.md) describe tested surfaces, not a universal guarantee. Missing independent delegation blocks the required review; self-review is not independent review.

Give each child the repository path, assigned profile path, proposed scope or exact base/head and dirty snapshot, acceptance criteria, relevant contract pointers and evidence to inspect. Require a complete read of `AGENTS.md` and its assigned profile before inspecting the diff, then the affected specifications/ADRs; a keyword search is not a complete read. Report unavailable child trace access as a visibility limitation rather than claiming to have inspected it. Do not pass the implementation conversation or desired verdict. Reviewers do not edit source, commit, publish or delegate; local fixtures and evidence are allowed within the task's permissions. These prompt limits are not an OS sandbox.

Allow at most three specialist children alongside the parent. Isolate browser sessions, databases, ports and output paths. Run performance measurements serially on an uncontended runner. Resolve findings, then re-evaluate scope and rerun affected reviews/checks after changes. A changed review input invalidates the affected conclusion.

Each review returns the role, base/head or proposed scope, files/contracts examined, findings with evidence and severity, missing coverage, and a verdict. Each executable check records the exact command, exit result, tested revision, environment and artifact/run link. Use `passed`, `failed`, `blocked`, `pending implementation` or `not applicable`; explain every non-pass. Design review passes do not imply runtime checks passed. Dirty snapshots cannot support final completion until rerun on the final committed revision.

The parent maps this evidence to the PR's acceptance criteria. All mandatory CI jobs must pass on the final PR revision. A Markdown report or checkbox does not enforce delegation or replace logs and required status checks. Report missing merge protection and carry its owner/admin configuration into check activation; do not claim deterministic routing from instructions alone.
