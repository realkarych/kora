## Impact

In one paragraph of 1–3 short sentences, explain the effect this PR delivers for the project, who it affects and why it matters where relevant, including material limits. Use plain language understandable without the implementation or prior discussion. Do not discuss code or implementation, list files or symbols, report checks, or narrate the work here. For design, documentation or process changes, describe their actual effect without implying implemented runtime behavior or inventing benefits. Keep technical details and required acceptance and verification evidence in the sections below.

Closes #

## What changes

## PR size

For a business-logic change, record the complete diff’s added plus deleted hand-written production-code line count, compared merge-base/head revisions and excluded test/documentation/generated paths. Mark not applicable with a reason for other changes.

- [ ] Business-logic changes contain at most 1,000 added plus deleted production-code lines; larger scopes were split into smaller, independently reviewable and mergeable PRs with linked sub-issues and explicit dependencies, preserving required tests, documentation, parity and verification.

## Merge

- [ ] The PR title reads as the subject of the one squash commit this PR becomes on `master`, which carries no body, as defined in [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#issues-and-pull-requests).

## Labels

- [ ] The PR and the issue it closes have exactly one matching primary `type:*` label, and their supplemental labels reflect their current scope and status, as defined in [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#labels).
- [ ] Saved GitHub labels have been verified; any deferred issue records a reason and revisit trigger. Labels do not replace owner approval of the plan.

## Acceptance criteria

| criterion | evidence |
|---|---|
|  |  |

## Documents updated

## Verification

Approved plan link and base revision:

Final reviewed/tested revision and working-tree state:

Follow [kora-verify](https://github.com/realkarych/kora/blob/master/.claude/skills/kora-verify/SKILL.md), the canonical router. Record the delivered outcome, work outside this change and stopping conditions. Keep one coverage table, including transitive contract/resource effects and integration ownership:

| Affected contract/component and changed risk | Integration owner | Blocking checks | Diagnostic measurements | Independent result or qualifying reuse |
|---|---|---|---|---|
| | | | | |

For selected performance scope, state the workload, resource owners/release conditions, approved numerical method and remaining limits. Diagnostics remain visible and establish no passing guarantee. A new experiment must answer a named unresolved acceptance question.

Record applicable observability impact and named evidence under [the logging contract](https://github.com/realkarych/kora/blob/master/knowledge/specs/observability.md), or a concrete applicability reason. Pending capabilities name their implementing issue and activation trigger.

Link each actual selected single-profile result with reviewer identity, inspected inputs/revision, bounded scope, findings and disposition. Carry matching results forward once; relevant inspected source/configuration changes reopen affected conclusions, while a new SHA alone does not. An implementation summary, request or generic clean verdict cannot prove independent review. Record justified descendant scope and retain native read-only restrictions.

| Check | Command or CI run/artifact link | Tested revision and result |
|---|---|---|
| | | |

Use passed, failed, blocked, pending implementation, unverified or not applicable, with a reason for every non-pass. Planning review does not prove runtime behavior. One successful complete final-head CI execution is required; local checks target changed inputs instead of duplicating the full suite. Do not require consecutive CI, repeated test-file or extra qualification campaigns for MVP merge. Preserve repetitions within existing tests, component safety assertions and scheduled/manual release qualification. Record whole-workflow duration against the ten-minute feedback target separately from hard job/test/cleanup deadlines and required results. Historical failures keep their verdicts. After two unsuccessful repair iterations for one problem, request an owner decision; unresolved required failures still block merge.

Native GitHub Codex Review uses the same coverage and assignment rules through `AGENTS.md`, retaining trusted-base and native read-only restrictions. Link actual scoped results when available; an aggregate clean verdict is not evidence for missing assignments or executable checks. Reuse does not waive a required native invocation or activate automatic review. For review-workflow changes, record repository-wide all-contributor/new-head automatic-review settings and required merge-status activation, or the precise configuration gap. Native subagent capability is assumed; no new capability experiment or internal-trace prerequisite is required.

- [ ] Scope was recomputed after fixes; affected checks and reviews cover the final change, and all mandatory CI jobs pass on the final PR revision.
- [ ] Changes to verification policy, gates or budgets preserve the stricter approved/base obligations unless the owner explicitly approved a reduction.
