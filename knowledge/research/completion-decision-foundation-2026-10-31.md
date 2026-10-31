# Pure completion-decision foundation

Date: 2026-10-31. Implementing issue: [#544](https://github.com/realkarych/kora/issues/544).

## Delivered boundary

`kora.engine.completion` provides `decide_attempt_completion` and `decide_run_completion`, using frozen internal values from `completion_models`. These are recommendations from checked facts, not transition entry points or reusable authority tokens. The module has no database, clock source, task, timer, sink, event, runtime call, artifact promotion, retry or release operation. Existing engine package permissions remain unchanged.

The rules come from current [scheduler pass 8 and run finish](../specs/scheduler.md#2-the-scheduling-loop), [deadline enforcement](../specs/scheduler.md#17-inherited-deadline-enforcement-adr-0022), [state machines](../specs/data-model.md#4-state-machines), [sticky expiry](../specs/data-model.md#152-sticky-expiry-and-current-result-authority) and ADR-0017/0018/0022. Closed historical issue #102 is not an alternate implementation contract.

## Checked facts and authority

Report shape, authenticated report admission and complete original-domain closure are independent facts. The report projection contains only outcome, allowed worker failure class and explicit validation/retirement flags, without error prose, details, payloads or raw evidence. `NoRecordedReport` differs from unavailable report facts. Only authoritative operator attestation permits no-report lost/cancelled completion from claimed, submitted or running; missing history alone never becomes lost. A finishing attempt already has an accepted immutable report, so finishing with `NoRecordedReport` is rejected at checked-facts construction before any outcome precedence. `ReportFactsUnavailable` continues to wait.

`ClosureFacts` is a projection supplied by the future transaction after checking affirmative runtime quiescence, every recorded dispatcher and settled publication. A shape-valid `TerminalReport`, a receipt, an empty dispatcher page, runtime exit or lease age cannot construct that proof by themselves. The helper performs no evidence binding or history lookup. Missing closure or unchecked report facts wait, including before timeout/cancellation terminal recommendations.

Every persistent ancestor and the own attempt deadline must be freshly checked under the future transaction's existing run lock. `DeadlineFactsUnavailable` waits. `UnlatchedExpiries` carries the complete checked due-owner set; its exact scope identities/cutoffs are returned without a write, latch acknowledgement or new transaction order. The producer applies the accepted shared expiry transitions, reloads all relevant facts and only then supplies `CheckedDeadlineState`. An earlier own latch does not hide a newly expired parent. Binding attribution remains earliest cutoff, with the existing run/outermost-wrapper/attempt tie rule; this module constructs no binding or clock.

Own timeout wins only for a still-current attempt with an own latch, eligible enclosing scopes and first cancel cause timeout. Earlier cancel/clear/supersede/stop prevents this classification; a later stop does not overwrite the first cause. Enclosing expiry and noncurrent identity yield cancellation without moving a replacement instance. Lost retains its admitted retirement prerequisite; failure preserves the accepted class. Success requires complete saved-contract output verification, with pending verification waiting and invalid/incomplete output failing artifact_invalid. Impossible checked combinations fail with one fixed content-free ValueError.

The run decision takes only checked run cutoff/latch and top-level unfinished/failure-presence facts. It finishes only running runs, never waits for attempt/recovery closure, and does not cancel independent branches after one failure. At or after the run cutoff it requests the existing run expiry transition, even with unfinished/unavailable top-level work. It cannot reopen a sticky latch after a backward clock change or retroactively alter an already finished run. The reader owns classification/count/sample; this function does not inspect node lists or create failed_nodes event data.

## Ownership and activation limits

Current database tables do not implement the normative write-once attempt cancel_cause/outcome fields or the full protocol deadline-binding format. This foundation chooses no column, JSON mapping, backfill, migration or reinterpretation of existing error/scratch data. The separate persistence owner must deliver an approved mapping before a production producer can supply these facts.

The existing `has_unfinished_top_level_instance` query and the separately owned failed_nodes reader supply exact top-level facts. They must run in the coherent snapshot used for application; partial pages or omitted checks cannot default to eligible/false/zero. The helper's required-expiry tuple has linear work in the supplied owner set and preserves its order; it neither traverses a graph nor chooses a new numerical bound. No cache or global collection retains inputs after the call. The caller owns the returned immutable values.

Production application still requires fresh identity/authority/report/deadline/output rechecks and atomic writes under existing locks, with the proper events, finished_at, discovery marker, replay/rollback behavior, artifact-state changes and exact capacity/target release. Pure tests do not establish those guarantees. Both SQLite/PostgreSQL integration, runtime/protected-domain evidence and real scheduler/API consumers remain pending with their implementing issues.

Pure helpers emit no diagnostic or durable event. Their fixed refusal propagates to the future owning boundary, which must activate its existing observability row, safe context/content, cancellation and sink-failure requirements. No HTTP operation, client/CLI/MCP parity or serving process is activated here.

## Verification evidence

The focused state-machine suite covers candidate/closure dimensions, first-cause and deadline precedence, complete due-owner handoff, report/admission/history separation, attestation/no-report, saved-output verification and separate top-level run outcome. Snapshot tests of report/latch order do not prove transaction interleavings. Tests use real immutable values and injected UTC observations, without mocks, provider calls, sleeps or database access.

Actual source review, final strict/architecture results and one complete final-head supported-platform CI are recorded in the pull request. Until those results return, implementation readiness is pending. No separate performance measurement, numerical gate, repeated qualification campaign or mutation campaign is selected for this pure prerequisite. The latest available mutation audit at planning time, run41025696626/source4cf962d, was partial and covered pricing/CEL paths; it supplied no completion finding or coverage.

Required later consumer tests include `test_success_pending_completion_is_rechecked`, `test_attempt_deadline_expiry_reaches_timeout`, `test_cancelled_attempt_reports_closure`, `test_lost_report_requires_retirement`, `test_run_finishes_while_superseded_attempt_unresolved`, `test_finish_at_deadline_is_not_success`, actual failed_nodes sampling and exact reservation/hold release. They remain unimplemented integration gates rather than passes attributed to this foundation.
