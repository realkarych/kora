## Impact

In one paragraph of 1–3 short sentences, explain the effect this PR delivers for the project, who it affects and why it matters where relevant, including material limits. Use plain language understandable without the implementation or prior discussion. Do not discuss code or implementation, list files or symbols, report checks, or narrate the work here. For design, documentation or process changes, describe their actual effect without implying implemented runtime behavior or inventing benefits. Keep technical details and required acceptance and verification evidence in the sections below.

Closes # (required except for a trivial editorial-only correction)

## What changes

## PR size

For a business-logic change, record the complete diff’s added plus deleted hand-written production-code line count, compared merge-base/head revisions and excluded test/documentation/generated paths. Mark not applicable with a reason for other changes.

- [ ] Business-logic changes contain at most 1,000 added plus deleted production-code lines; larger scopes were split into smaller, independently reviewable and mergeable PRs with linked sub-issues and explicit dependencies, or have a linked prior owner-approved indivisible-change exception. Required tests, documentation, parity and verification remain complete.

## Merge

- [ ] The PR title reads as the subject of the one squash commit this PR becomes on `master`, which carries no body, as defined in [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#issues-and-pull-requests).

## Labels

- [ ] The PR and, when required, the issue it closes have exactly one matching primary `type:*` label, and their supplemental labels reflect their current scope and status, as defined in [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#labels).
- [ ] Saved GitHub labels have been verified; any deferred issue records a reason and revisit trigger. Labels describe the work. Record the applicable approval basis in Verification.

## Acceptance criteria

| criterion | evidence |
|---|---|
|  |  |

## Documents updated

## Verification

Base revision and plan link, if required. Record individual owner approval when required, explain why standing authorization applies, or state why a plan is not required:

Final reviewed/tested revision and working-tree state:

Follow [kora-verify](https://github.com/realkarych/kora/blob/master/.claude/skills/kora-verify/SKILL.md), the canonical router. Record the delivered outcome, work outside this change and stopping conditions. Keep one coverage table, including transitive contract/resource effects and integration ownership:

| Affected contract/component and changed risk | Integration owner | Blocking checks | Diagnostic measurements | Independent result or qualifying reuse |
|---|---|---|---|---|
| | | | | |

For selected performance scope, state the workload, resource owners/release conditions, approved numerical method and remaining limits. Diagnostics remain visible and establish no passing guarantee. A new experiment must answer a named unresolved acceptance question.

Record applicable observability impact and named evidence under [the logging contract](https://github.com/realkarych/kora/blob/master/knowledge/specs/observability.md), or a concrete applicability reason. Pending capabilities name their implementing issue and activation trigger.

Link each actual selected profile result with reviewer identity, inspected inputs/revision, bounded scope, findings and disposition. An implementation summary, request or generic clean verdict cannot prove independent review. Record justified descendant scope. Where [kora-verify](https://github.com/realkarych/kora/blob/master/.claude/skills/kora-verify/SKILL.md#select-independent-reviews) let one reviewer apply both correctness and security, record each profile's conclusion separately. [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#development-verification) and [kora-verify](https://github.com/realkarych/kora/blob/master/.claude/skills/kora-verify/SKILL.md#dispatch-and-collect-evidence) state when a result qualifies, carries forward or reopens.

| Check | Command or CI run/artifact link | Tested revision and result |
|---|---|---|
| | | |

Use passed, failed, blocked, pending implementation, unverified or not applicable, with a reason for every non-pass. Keep this section current for the latest head: each earlier failed CI execution or other recorded failed check stays as one line with its run link or command, head, each directly failing or cancelled job, classification and owning issue (or this pull request when fixed here); jobs whose only failures are checks of the results of jobs they depend on are summarized in one sentence, and every other refusal is listed. For a CI infrastructure rerun, which keeps every per-job entry, record what [AGENTS.md](https://github.com/realkarych/kora/blob/master/AGENTS.md#development-verification) requires: the failed run and attempt, its commit, each failed or cancelled job with its conclusion and the annotation, log line or incident report behind its classification, and the rerun attempt. Record whole-workflow duration against the ten-minute feedback target separately from hard job/test/cleanup deadlines and required results. The final-head CI requirement, MVP merge bounds, historical failures and repair escalation follow AGENTS.md [Development verification](https://github.com/realkarych/kora/blob/master/AGENTS.md#development-verification), [Testing](https://github.com/realkarych/kora/blob/master/AGENTS.md#testing) and [Definition of done](https://github.com/realkarych/kora/blob/master/AGENTS.md#definition-of-done).

For native GitHub Codex Review, link actual scoped results when available. For review-workflow changes, record repository-wide all-contributor/new-head automatic-review settings and required merge-status activation, or the precise configuration gap. Native review follows the [Code Review Rules](https://github.com/realkarych/kora/blob/master/AGENTS.md#code-review-rules) of AGENTS.md and the [GitHub reviewer mode](https://github.com/realkarych/kora/blob/master/.claude/skills/kora-verify/SKILL.md#github-reviewer-mode) of kora-verify.

- [ ] Scope was recomputed after fixes; affected checks and reviews cover the final change, and all mandatory CI jobs pass on the final PR revision.
- [ ] Changes to verification policy, gates or budgets preserve the stricter approved/base obligations unless the owner explicitly approved a reduction.
