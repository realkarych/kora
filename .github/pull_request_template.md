## Impact

In one paragraph of 1–3 short sentences, explain the effect this PR delivers for the project, who it affects and why it matters where relevant, including material limits. Use plain language understandable without the implementation or prior discussion. Do not discuss code or implementation, list files or symbols, report checks, or narrate the work here. For design, documentation or process changes, describe their actual effect without implying implemented runtime behavior or inventing benefits. Keep technical details and required acceptance and verification evidence in the sections below.

Closes #

## What changes

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

Affected components and contracts, including semantic effects beyond file paths. Split component families by contract/resource ownership and follow transitive effects until an unchanged boundary stops propagation. Keep complete component/profile coverage and map every applicable profile to an assignment identifier:

| Component | Direct change or transitive causal edge, with contract/source evidence | Integration-boundary owner | Applicable profiles and checks, or reason for exclusion | Assignment identifiers by profile |
|---|---|---|---|---|
| | | | | |

Performance/resource lifetime: workload, acquired resources and release conditions, repeated success/failure/cancellation/recovery/shutdown cycles, quiescence, peak/retained measurements and baseline/budget evidence; give a concrete applicability reason for instruction/editorial-only changes.

Observability impact and evidence under [the logging contract](https://github.com/realkarych/kora/blob/master/knowledge/specs/observability.md): event/level/context ownership, safe fields, stream separation and bounded failure behavior; link named tests or give a concrete applicability reason. Pending component rows must name their implementing issue and activation trigger. Logging collection, redaction, propagation, buffer or sink changes require correctness, security and performance review.

One row per independent single-profile assignment. Related components may share a bounded assignment with an explicit common contract and grouping reason; different profiles use separate independent contexts. kora-verify owns the review process: map qualifying step, local or native results to these assignments rather than adding duplicate stages. Verify actual independent result provenance, scope and inputs; a coordinator-authored report or generic clean verdict is insufficient. Record reassessment of changed inputs and affected conclusions, actual returned results and justified descendant splits; requested/running assignments are not completed reviews.

| Assignment / profile | Covered components, boundaries and grouping reason | Reviewer identity and justified descendant scope | Reviewed inputs/revision, reassessment and evidence | Verdict, findings and disposition or applicability reason |
|---|---|---|---|---|
| | | | | |

| Check | Command or CI run/artifact link | Tested revision and result |
|---|---|---|
| | | |

Use passed, failed, blocked, pending implementation, unverified or not applicable, with a reason for every non-pass. For pending checks, link the implementing issue and activation trigger. A design review is not runtime evidence. Record missing required merge statuses and their owner/admin activation work; a report does not replace CI logs or prove independent dispatch.

Native GitHub Codex Review uses the same coverage and assignment rules through `AGENTS.md`, retaining trusted-base and native read-only restrictions. Link actual scoped results when available; an aggregate clean verdict is not evidence for missing assignments or executable checks. Reuse does not waive a required native invocation or activate automatic review. For review-workflow changes, record repository-wide all-contributor/new-head automatic-review settings and required merge-status activation, or the precise configuration gap. Native subagent capability is assumed; no new capability experiment or internal-trace prerequisite is required.

- [ ] Scope was recomputed after fixes; affected checks and reviews cover the final change, and all mandatory CI jobs pass on the final PR revision.
- [ ] Changes to verification policy, gates or budgets preserve the stricter approved/base obligations unless the owner explicitly approved a reduction.
