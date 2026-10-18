Closes #

## What changes

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

Affected components and contracts, including semantic effects beyond file paths:

Observability impact and evidence under [the logging contract](https://github.com/realkarych/kora/blob/master/knowledge/specs/observability.md): event/level/context ownership, safe fields, stream separation and bounded failure behavior; link named tests or give a concrete applicability reason. Pending component rows must name their implementing issue and activation trigger. Logging collection, redaction, propagation, buffer or sink changes require correctness, security and performance review.

| Independent review | Reviewed revision/scope and evidence link | Findings and disposition or applicability reason |
|---|---|---|
| Correctness | | |
| Browser | | |
| Performance | | |
| Security | | |

| Check | Command or CI run/artifact link | Tested revision and result |
|---|---|---|
| | | |

Use passed, failed, blocked, pending implementation or not applicable, with a reason for every non-pass. For pending checks, link the implementing issue and activation trigger. A design review is not runtime evidence. Record missing required merge statuses and their owner/admin activation work; a report does not replace CI logs or prove independent dispatch.

Native GitHub Codex Review uses the same skill through `AGENTS.md`. Link its result when available; a single native pass does not replace the specialist reviews or executable evidence above.

- [ ] Scope was recomputed after fixes; affected checks and reviews cover the final change, and all mandatory CI jobs pass on the final PR revision.
- [ ] Changes to verification policy, gates or budgets preserve the stricter approved/base obligations unless the owner explicitly approved a reduction.
