# Inherited execution deadline design and review record

Date: 2026-10-17. Scope: issue #57, [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md), the deadline specifications and affected lifecycle descriptions. Direction and implementation plan accepted by the project owner; document wording pending owner review. This is a design-only change, not runtime implementation or measured termination evidence.

## Necessity and selected scope

Independent analyses compared leaving the issue unchanged, downgrading enclosing timeouts to scheduler cancellation intent, and inheriting known deadlines through existing lifecycle control. The selected minimum preserves useful autonomous stopping during control-plane partitions without another service or renewable lease. ADR-0017 already addresses safe replacement and reservation retention; it does not stop an existing runner at an enclosing cutoff.

The canonical SQL graph's six-hour retrying wrapper and two-hour child make parent precedence a normal scenario. The change defines scope starts, immutable deadline inheritance, phase guards, fixed nominal grace, clock assumptions and future acceptance evidence. Runtime enforcement remains part of the first execution slice.

## Architecture findings and dispositions

| Finding | Disposition |
|---|---|
| A minimum added only to Workload misses host preparation before submit and host publication | Carry the saved binding in the authenticated control context and original runtime registry/common executor before effects; cover every managed placement |
| A node-only or renewed retry clock can escape an expired parent | Preserve run/wrapper starts across iterations/clear; distinguish a new physical attempt and a newly materialized nested wrapper from existing ancestors |
| Retrying a wrapper must check its own persistent deadline, while an old physical timeout must not prohibit an eligible new attempt | Pre-mutation checks include the target wrapper and all persistent ancestors; previous physical attempt clock is excluded |
| Deadline/grace after reconnect can accidentally restart | Both nominal thresholds derive from original UTC D; sticky observed expiry and fixed D+30s survive adoption |
| A required finite scheduling-latency margin is not established on ordinary supported operating systems | State nominal local actions subject to scheduling/runtime-control latency; retain only an explicit clock/conversion assumption, no exact dispatch/termination promise |
| Timeout-pass delay can accept a late current result | Existing shared authority/completion transitions compare scope deadlines/latches under the run lock and touch run.updated_at with timeout intent |
| Prepared-checkpoint recovery can initiate post-expiry publication | Limit new push/change-request authorization to pre-expiry; retain only read-only reconciliation and closure afterward |
| A timer can be mistaken for termination evidence | Preserve ADR-0017/ADR-0020 execution, dispatcher and publication closure barriers and acquired holds |

No new public operation/tool, IR authoring field, dependency, UI action, clock service or runtime test scaffold is introduced. Blocking/noncooperative local control, suspension and lost registry do not gain a physical-stop guarantee.

## Verification and whole-branch review

The future named release contracts are [data-model section 15.3](../specs/data-model.md#153-recovery-and-future-acceptance-contracts); they require configured conforming fakes, injected time, explicit barriers, both metadata databases and separate actual local/Docker process evidence. Documentation CI does not run them.

Whole-branch architecture/lifecycle and cross-document/minimum-scope reviews, actual documentation/schema CI results and final dispositions will be recorded in the pull request before owner wording approval. The initial local CI set passed: Markdown lint, Lychee links/anchors (2774 total, 1239 OK, 1535 existing exclusions, zero errors), all Mermaid diagrams, both IR examples and both GitHub template schemas. These checks establish document/schema structure, not runtime behavior. Whole-branch review and exact-head GitHub CI are pending.

Two independent whole-branch reviews inspected commit `39357771a9e33c62981ee75effb4f4b95520f9b3` against the merged ADR-0021 base. The architecture/lifecycle review reported no findings. The consistency review found one Important recovery contradiction: the old scheduler recipe and dispatcher-generation test still allowed checkpoint continuation after expiry. Their original paragraphs/test and the teaching recovery row now require the unchanged eligible deadline/expiry guard and specify read-only closure after expiry.

The consistency review also identified three remaining exact-stop descriptions in the [pre-consolidation internals](https://github.com/realkarych/kora/blob/1baf9891af1d8dc69e4e33dd7bc2af22237072c5/knowledge/architecture/internals.md) introduction/table/glossary. These were graded Important for contract consistency, rather than cosmetic wording, because they overstated the accepted nominal-action guarantee; all now describe interruption/escalation and separately evidenced physical closure. No Minor findings are deferred. The executor made one consolidated correction pass; no second independent review of those fixes is claimed. Full actual CI is rerun on the final sources.

The reviewers did not establish actual runtime/provider cancellation or OS containment, a particular installation's clock assumptions, or future executable tests; these remain explicit release gates. Broader matching/pass ordering remains issue #48 as approved. One reviewer independently ran `git diff --check`; the executor owns the full documentation/schema CI and exact-head GitHub evidence recorded in the pull request. Neither a document review nor green documentation CI proves physical stopping.
