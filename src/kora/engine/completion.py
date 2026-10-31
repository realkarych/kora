from kora.engine.completion_models import (
    AttemptCompletionFacts,
    AttemptDecision,
    AttemptTerminalDecision,
    AwaitCompletionPrerequisite,
    DeadlineFactsUnavailable,
    ExpiryOwner,
    ExpiryTransitionsRequired,
    NoCompletion,
    NoRecordedReport,
    ReportFactsUnavailable,
    RunCompletionFacts,
    RunDecision,
    RunTerminalDecision,
    UnlatchedExpiries,
    require_completion_fact,
)
from kora.ir import ErrorClass


def decide_attempt_completion(facts: AttemptCompletionFacts) -> AttemptDecision:
    if facts.status != "finishing" and not (
        facts.status in {"claimed", "submitted", "running"}
        and facts.operator_attested_closure
    ):
        return NoCompletion()
    closure = facts.closure
    if closure is None or not (
        closure.runtime_quiescent
        and closure.all_dispatchers_closed
        and closure.publication_state != "unknown"
    ):
        return AwaitCompletionPrerequisite("closure")
    deadlines = facts.deadlines
    if isinstance(deadlines, DeadlineFactsUnavailable):
        return AwaitCompletionPrerequisite("deadlines")
    if isinstance(deadlines, UnlatchedExpiries):
        return ExpiryTransitionsRequired(
            tuple(
                ExpiryOwner(item.scope_kind, item.scope_id, item.at)
                for item in deadlines.deadlines
            )
        )
    report = facts.report
    if isinstance(report, ReportFactsUnavailable):
        return AwaitCompletionPrerequisite("report")
    if isinstance(report, NoRecordedReport):
        if not facts.operator_attested_closure:
            return AwaitCompletionPrerequisite("report")
    elif not (
        report.shape_validated
        and report.admission_validated
        and (report.outcome != "lost" or report.retirement_verified)
    ):
        return AwaitCompletionPrerequisite("report")
    if deadlines.own_expiry_latched:
        require_completion_fact(facts.cancel_cause is not None)
    if (
        deadlines.own_expiry_latched
        and facts.is_current_attempt
        and deadlines.enclosing_scopes_eligible
        and facts.cancel_cause == "timeout"
    ):
        return AttemptTerminalDecision("failed", ErrorClass.TIMEOUT, True)
    cancelled = (
        facts.desired_cancel
        or facts.stop_requested
        or not facts.is_current_attempt
        or not deadlines.enclosing_scopes_eligible
    )
    if cancelled:
        return AttemptTerminalDecision("cancelled", None, facts.is_current_attempt)
    require_completion_fact(facts.cancel_cause is None)
    if isinstance(report, NoRecordedReport):
        return AttemptTerminalDecision("lost", ErrorClass.INFRASTRUCTURE, True)
    require_completion_fact(report.outcome != "cancelled")
    if report.outcome == "lost":
        return AttemptTerminalDecision("lost", ErrorClass.INFRASTRUCTURE, True)
    if report.outcome == "failed":
        return AttemptTerminalDecision("failed", report.error_class, True)
    if facts.outputs == "pending":
        return AwaitCompletionPrerequisite("outputs")
    if facts.outputs == "invalid_or_incomplete":
        return AttemptTerminalDecision("failed", ErrorClass.ARTIFACT_INVALID, True)
    return AttemptTerminalDecision("succeeded", None, True)


def decide_run_completion(facts: RunCompletionFacts) -> RunDecision:
    if facts.status != "running":
        return NoCompletion()
    if not facts.deadline_checked:
        return AwaitCompletionPrerequisite("deadlines")
    require_completion_fact(facts.deadline_expired_at is None)
    if facts.deadline_at is not None and facts.observed_at >= facts.deadline_at:
        return ExpiryTransitionsRequired(
            (ExpiryOwner("run", facts.run_id, facts.deadline_at),)
        )
    if not facts.top_level_checked or facts.has_unfinished_top_level:
        return AwaitCompletionPrerequisite("top_level")
    return RunTerminalDecision("failed" if facts.has_failed_top_level else "succeeded")
