from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from kora.engine.completion import decide_attempt_completion, decide_run_completion
from kora.engine.completion_models import (
    AttemptCompletionFacts,
    AttemptStatus,
    AttemptTerminalDecision,
    AwaitCompletionPrerequisite,
    CancelCause,
    CheckedDeadlineState,
    ClosureFacts,
    DeadlineFactsUnavailable,
    ExpiryOwner,
    ExpiryTransitionsRequired,
    NoCompletion,
    NoRecordedReport,
    OutputVerification,
    PublicationState,
    RecordedReportFacts,
    ReportEvaluation,
    ReportFactsUnavailable,
    ReportOutcome,
    RunCompletionFacts,
    RunStatus,
    RunTerminalDecision,
    UnlatchedExpiries,
)
from kora.ir import ErrorClass
from kora.protocols.deadlines import ExecutionDeadline, ScopeKind
from kora.protocols.evidence import (
    ClosureEvidence,
    EvidenceScope,
    NormalCompletionQuiescence,
    PublicationOutcome,
)
from kora.protocols.report import TerminalReport

NOW: datetime = datetime(2026, 1, 1, tzinfo=UTC)


def report_facts(
    outcome: ReportOutcome = "succeeded",
    error_class: ErrorClass | None = None,
) -> RecordedReportFacts:
    return RecordedReportFacts(outcome, error_class, True, True, outcome == "lost")


def attempt_facts() -> AttemptCompletionFacts:
    return AttemptCompletionFacts(
        status="finishing",
        is_current_attempt=True,
        desired_cancel=False,
        stop_requested=False,
        cancel_cause=None,
        operator_attested_closure=False,
        closure=ClosureFacts(True, True, "not_applicable"),
        report=report_facts(),
        outputs="complete_valid",
        deadlines=CheckedDeadlineState(False, True),
    )


def run_facts() -> RunCompletionFacts:
    return RunCompletionFacts(
        run_id="run_1",
        status="running",
        observed_at=NOW,
        deadline_at=NOW + timedelta(seconds=1),
        deadline_expired_at=None,
        deadline_checked=True,
        top_level_checked=True,
        has_unfinished_top_level=False,
        has_failed_top_level=False,
    )


def deadline(kind: ScopeKind, scope_id: str, at: datetime = NOW) -> ExecutionDeadline:
    return ExecutionDeadline(at, kind, scope_id, timedelta(0), timedelta(seconds=30))


@pytest.mark.parametrize(
    "status",
    [
        "created",
        "claimed",
        "submitted",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "lost",
    ],
)
def test_only_eligible_attempts_enter_completion(status: AttemptStatus) -> None:
    facts = replace(attempt_facts(), status=status)

    decision = decide_attempt_completion(facts)

    assert decision == NoCompletion()


@pytest.mark.parametrize(
    "closure",
    [
        None,
        ClosureFacts(False, True, "completed"),
        ClosureFacts(True, False, "completed"),
        ClosureFacts(True, True, "unknown"),
    ],
)
def test_incomplete_aggregate_closure_blocks_timeout_and_lost(
    closure: ClosureFacts | None,
) -> None:
    facts = replace(
        attempt_facts(),
        closure=closure,
        report=report_facts("lost"),
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=CheckedDeadlineState(True, True),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AwaitCompletionPrerequisite("closure")


@pytest.mark.parametrize(
    "state", ["completed", "failed", "not_dispatched", "not_applicable"]
)
def test_each_settled_publication_state_allows_checked_completion(
    state: PublicationState,
) -> None:
    facts = replace(attempt_facts(), closure=ClosureFacts(True, True, state))

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("succeeded", None, True)


def test_unchecked_ancestor_guards_never_become_eligible_defaults() -> None:
    facts = replace(attempt_facts(), deadlines=DeadlineFactsUnavailable())

    decision = decide_attempt_completion(facts)

    assert decision == AwaitCompletionPrerequisite("deadlines")


def test_all_unlatched_owners_are_returned_without_ordering_or_mutation() -> None:
    deadlines = (
        deadline("attempt", "attempt_1"),
        deadline("run", "run_1"),
        deadline("subgraph", "wrapper_1"),
    )
    facts = replace(attempt_facts(), deadlines=UnlatchedExpiries(NOW, deadlines))
    original = facts

    decision = decide_attempt_completion(facts)

    assert decision == ExpiryTransitionsRequired(
        tuple(
            ExpiryOwner(item.scope_kind, item.scope_id, item.at) for item in deadlines
        )
    )
    assert facts == original
    assert facts.deadlines == UnlatchedExpiries(NOW, deadlines)


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "succeeded"])
def test_own_timeout_result_is_independent_of_recorded_outcome(
    outcome: ReportOutcome,
) -> None:
    report = report_facts(outcome, ErrorClass.TIMEOUT if outcome == "failed" else None)
    facts = replace(
        attempt_facts(),
        report=report,
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=CheckedDeadlineState(True, True),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("failed", ErrorClass.TIMEOUT, True)


def test_report_and_latch_order_require_reload_before_the_same_timeout_decision() -> (
    None
):
    reported = replace(
        attempt_facts(),
        deadlines=UnlatchedExpiries(NOW, (deadline("attempt", "attempt_1"),)),
    )
    latched = replace(
        attempt_facts(),
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=CheckedDeadlineState(True, True),
        report=ReportFactsUnavailable(),
    )
    reloaded = replace(latched, report=report_facts("cancelled"))

    before_latch = decide_attempt_completion(reported)
    before_report = decide_attempt_completion(latched)
    after_reload = decide_attempt_completion(reloaded)

    assert before_latch == ExpiryTransitionsRequired(
        (ExpiryOwner("attempt", "attempt_1", NOW),)
    )
    assert before_report == AwaitCompletionPrerequisite("report")
    assert after_reload == AttemptTerminalDecision("failed", ErrorClass.TIMEOUT, True)


@pytest.mark.parametrize("cause", ["cancel", "clear", "supersede", "stop"])
def test_prior_cancellation_cause_beats_own_timeout(cause: CancelCause) -> None:
    facts = replace(
        attempt_facts(),
        desired_cancel=True,
        cancel_cause=cause,
        deadlines=CheckedDeadlineState(True, True),
        report=report_facts("cancelled"),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("cancelled", None, True)


def test_later_stop_cannot_replace_first_own_timeout() -> None:
    facts = replace(
        attempt_facts(),
        stop_requested=True,
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=CheckedDeadlineState(True, True),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("failed", ErrorClass.TIMEOUT, True)


def test_newly_due_parent_is_not_hidden_by_earlier_own_latch() -> None:
    parent_deadline = deadline("run", "run_1", NOW + timedelta(seconds=1))
    facts = replace(
        attempt_facts(),
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=UnlatchedExpiries(parent_deadline.at, (parent_deadline,)),
    )
    reloaded = replace(facts, deadlines=CheckedDeadlineState(True, False))

    before_parent_expiry = decide_attempt_completion(facts)
    after_parent_expiry = decide_attempt_completion(reloaded)

    assert before_parent_expiry == ExpiryTransitionsRequired(
        (ExpiryOwner("run", "run_1", parent_deadline.at),)
    )
    assert after_parent_expiry == AttemptTerminalDecision("cancelled", None, True)


@pytest.mark.parametrize(
    ("current", "desired", "stop", "cause", "enclosing"),
    [
        (True, True, False, "cancel", True),
        (True, False, True, "stop", True),
        (False, False, False, "supersede", True),
        (True, False, False, "timeout", False),
    ],
)
def test_cancellation_precedes_recorded_success(
    current: bool,
    desired: bool,
    stop: bool,
    cause: CancelCause,
    enclosing: bool,
) -> None:
    facts = replace(
        attempt_facts(),
        is_current_attempt=current,
        desired_cancel=desired,
        stop_requested=stop,
        cancel_cause=cause,
        deadlines=CheckedDeadlineState(False, enclosing),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("cancelled", None, current)


def test_superseded_own_timeout_never_moves_replacement_instance() -> None:
    facts = replace(
        attempt_facts(),
        is_current_attempt=False,
        desired_cancel=True,
        cancel_cause="timeout",
        deadlines=CheckedDeadlineState(True, True),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("cancelled", None, False)


@pytest.mark.parametrize(
    "report",
    [
        ReportFactsUnavailable(),
        replace(report_facts(), shape_validated=False),
        replace(report_facts(), admission_validated=False),
        replace(report_facts("lost"), retirement_verified=False),
    ],
)
def test_unverified_report_facts_cannot_terminalize_even_cancelled_attempt(
    report: ReportEvaluation,
) -> None:
    facts = replace(
        attempt_facts(), report=report, desired_cancel=True, cancel_cause="cancel"
    )

    decision = decide_attempt_completion(facts)

    assert decision == AwaitCompletionPrerequisite("report")


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.HARNESS_ERROR,
        ErrorClass.COMMAND_FAILED,
        ErrorClass.TIMEOUT,
        ErrorClass.EVENTS_EXHAUSTED,
        ErrorClass.BUDGET_EXHAUSTED,
        ErrorClass.ARTIFACT_INVALID,
        ErrorClass.STALLED,
        ErrorClass.PROVIDER_LIMITED,
        ErrorClass.PROTECTED_PATH_MODIFIED,
        ErrorClass.CREDENTIAL_LEAK,
        ErrorClass.INTEGRITY_ERROR,
        ErrorClass.INFRASTRUCTURE,
        ErrorClass.CONFIG_ERROR,
    ],
)
def test_accepted_worker_failure_class_is_preserved(error_class: ErrorClass) -> None:
    facts = replace(attempt_facts(), report=report_facts("failed", error_class))

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("failed", error_class, True)


@pytest.mark.parametrize(
    "outputs", ["pending", "invalid_or_incomplete", "complete_valid"]
)
def test_success_requires_complete_saved_contract_output_verification(
    outputs: OutputVerification,
) -> None:
    facts = replace(attempt_facts(), outputs=outputs)
    expected = {
        "pending": AwaitCompletionPrerequisite("outputs"),
        "invalid_or_incomplete": AttemptTerminalDecision(
            "failed", ErrorClass.ARTIFACT_INVALID, True
        ),
        "complete_valid": AttemptTerminalDecision("succeeded", None, True),
    }[outputs]

    decision = decide_attempt_completion(facts)

    assert decision == expected


def test_accepted_lost_requires_retirement_and_complete_closure() -> None:
    facts = replace(attempt_facts(), report=report_facts("lost"))

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision("lost", ErrorClass.INFRASTRUCTURE, True)


@pytest.mark.parametrize("status", ["claimed", "submitted", "running"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_attested_no_report_completion_keeps_cancellation_distinct_from_lost(
    status: AttemptStatus,
    cancelled: bool,
) -> None:
    facts = replace(
        attempt_facts(),
        status=status,
        operator_attested_closure=True,
        report=NoRecordedReport(),
        desired_cancel=cancelled,
        cancel_cause="cancel" if cancelled else None,
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision(
        "cancelled" if cancelled else "lost",
        None if cancelled else ErrorClass.INFRASTRUCTURE,
        True,
    )


def test_unattested_pre_report_attempt_is_not_lost() -> None:
    facts = replace(attempt_facts(), status="running", report=NoRecordedReport())

    decision = decide_attempt_completion(facts)

    assert decision == NoCompletion()


@pytest.mark.parametrize("attested", [False, True])
@pytest.mark.parametrize("cause", [None, "cancel", "timeout"])
def test_finishing_requires_recorded_report_before_outcome_precedence(
    attested: bool,
    cause: CancelCause | None,
) -> None:
    facts = replace(
        attempt_facts(),
        operator_attested_closure=attested,
        desired_cancel=cause is not None,
        cancel_cause=cause,
        deadlines=CheckedDeadlineState(cause == "timeout", True),
    )

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        replace(facts, report=NoRecordedReport())

    assert str(refused.value) == "completion facts are invalid"


@pytest.mark.parametrize("status", ["finishing", "running"])
def test_attested_unknown_report_still_waits(status: AttemptStatus) -> None:
    facts = replace(
        attempt_facts(),
        status=status,
        operator_attested_closure=True,
        desired_cancel=True,
        cancel_cause="cancel",
        report=ReportFactsUnavailable(),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AwaitCompletionPrerequisite("report")


def test_attestation_does_not_replace_the_recorded_outcome() -> None:
    facts = replace(
        attempt_facts(),
        operator_attested_closure=True,
        report=report_facts("failed", ErrorClass.COMMAND_FAILED),
    )

    decision = decide_attempt_completion(facts)

    assert decision == AttemptTerminalDecision(
        "failed", ErrorClass.COMMAND_FAILED, True
    )


def test_shape_valid_report_is_neither_admission_nor_aggregate_closure() -> None:
    report = TerminalReport(
        outcome="succeeded",
        error_class=None,
        error=None,
        lost_reason=None,
        evidence=ClosureEvidence(
            runtime=NormalCompletionQuiescence(
                kind="normal_completion",
                attempt_id="attempt_1",
                execution_domain_id="domain_1",
                record_ref="record_1",
                observed_at=NOW,
                scope=EvidenceScope.FAKE_RUNTIME,
            ),
            dispatchers=(),
            publication=PublicationOutcome(
                state="not_applicable", operation_id=None, ref=None, code=None
            ),
        ),
    )
    projection = RecordedReportFacts(
        report.outcome, report.error_class, True, False, False
    )
    unclosed = replace(
        attempt_facts(),
        report=projection,
        closure=ClosureFacts(True, False, "not_applicable"),
    )
    closed = replace(unclosed, closure=ClosureFacts(True, True, "not_applicable"))

    lacking_history = decide_attempt_completion(unclosed)
    lacking_admission = decide_attempt_completion(closed)

    assert lacking_history == AwaitCompletionPrerequisite("closure")
    assert lacking_admission == AwaitCompletionPrerequisite("report")


@pytest.mark.parametrize(
    "facts",
    [
        replace(attempt_facts(), deadlines=CheckedDeadlineState(True, True)),
        replace(attempt_facts(), report=report_facts("cancelled")),
        replace(attempt_facts(), cancel_cause="stop"),
    ],
)
def test_impossible_checked_snapshots_fail_without_product_outcome(
    facts: AttemptCompletionFacts,
) -> None:
    expected = "completion facts are invalid"

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        decide_attempt_completion(facts)

    assert str(refused.value) == expected


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.ATTEMPT_FAILED,
        ErrorClass.APPROVAL_REJECTED,
        ErrorClass.APPROVAL_EXPIRED,
        ErrorClass.CANDIDATE_MOVED,
        ErrorClass.WAIT_EXPIRED,
        ErrorClass.CANCELLED,
    ],
)
def test_control_plane_classes_cannot_become_accepted_worker_failures(
    error_class: ErrorClass,
) -> None:
    outcome: ReportOutcome = "failed"

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        report_facts(outcome, error_class)

    assert str(refused.value) == "completion facts are invalid"


@pytest.mark.parametrize("status", ["pending", "succeeded", "failed", "cancelled"])
def test_only_running_runs_finish_without_reopening_historical_success(
    status: RunStatus,
) -> None:
    facts = replace(
        run_facts(),
        status=status,
        observed_at=NOW + timedelta(days=1),
        deadline_expired_at=NOW,
    )

    decision = decide_run_completion(facts)

    assert decision == NoCompletion()


def test_unknown_run_guard_is_not_an_unlimited_default() -> None:
    facts = replace(run_facts(), deadline_checked=False, deadline_at=None)

    decision = decide_run_completion(facts)

    assert decision == AwaitCompletionPrerequisite("deadlines")


@pytest.mark.parametrize("difference", [timedelta(0), timedelta(seconds=1)])
def test_due_run_expiry_precedes_finish_and_pending_top_level_facts(
    difference: timedelta,
) -> None:
    facts = replace(
        run_facts(),
        deadline_at=NOW,
        observed_at=NOW + difference,
        top_level_checked=False,
        has_unfinished_top_level=True,
    )

    decision = decide_run_completion(facts)

    assert decision == ExpiryTransitionsRequired((ExpiryOwner("run", "run_1", NOW),))


@pytest.mark.parametrize("checked", [False, True])
def test_unchecked_or_unfinished_top_level_work_does_not_fail_fast(
    checked: bool,
) -> None:
    facts = replace(
        run_facts(),
        top_level_checked=checked,
        has_unfinished_top_level=True,
        has_failed_top_level=True,
    )

    decision = decide_run_completion(facts)

    assert decision == AwaitCompletionPrerequisite("top_level")


def test_successful_cleanup_cannot_mask_top_level_failure() -> None:
    facts = replace(run_facts(), has_failed_top_level=True)

    decision = decide_run_completion(facts)

    assert decision == RunTerminalDecision("failed")


def test_all_finished_nonfailing_including_skipped_permit_success_without_limit() -> (
    None
):
    facts = replace(run_facts(), deadline_at=None)

    decision = decide_run_completion(facts)

    assert decision == RunTerminalDecision("succeeded")


def test_unresolved_superseded_attempt_does_not_hold_top_level_run_outcome() -> None:
    attempt = replace(attempt_facts(), is_current_attempt=False, closure=None)
    run = replace(run_facts(), has_failed_top_level=True)

    attempt_decision = decide_attempt_completion(attempt)
    run_decision = decide_run_completion(run)

    assert attempt_decision == AwaitCompletionPrerequisite("closure")
    assert run_decision == RunTerminalDecision("failed")


def test_retry_reentry_rechecks_top_level_work() -> None:
    settled = replace(run_facts(), status="failed", has_failed_top_level=True)
    retrying = replace(settled, status="running", has_unfinished_top_level=True)
    finished = replace(
        retrying, has_unfinished_top_level=False, has_failed_top_level=False
    )

    before_retry = decide_run_completion(settled)
    during_retry = decide_run_completion(retrying)
    after_retry = decide_run_completion(finished)

    assert before_retry == NoCompletion()
    assert during_retry == AwaitCompletionPrerequisite("top_level")
    assert after_retry == RunTerminalDecision("succeeded")


def test_sticky_expiry_cannot_be_reopened_by_backward_clock() -> None:
    facts = replace(
        run_facts(), observed_at=NOW - timedelta(days=1), deadline_expired_at=NOW
    )

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        decide_run_completion(facts)

    assert str(refused.value) == "completion facts are invalid"


@pytest.mark.parametrize("at", [NOW + timedelta(microseconds=1)])
def test_unobserved_future_expiry_is_not_a_latch_request(at: datetime) -> None:
    pending = deadline("run", "run_1", at)

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        UnlatchedExpiries(NOW, (pending,))

    assert str(refused.value) == "completion facts are invalid"


def test_duplicate_expiry_owner_is_refused_without_picking_a_deadline() -> None:
    pending = (
        deadline("run", "run_1"),
        deadline("run", "run_1", NOW - timedelta(seconds=1)),
    )

    with pytest.raises(ValueError, match="^completion facts are invalid$") as refused:
        UnlatchedExpiries(NOW, pending)

    assert str(refused.value) == "completion facts are invalid"


def attempt_to_mutate(value: object, name: str) -> None:
    setattr(value, name, False)


def test_decisive_facts_and_results_are_frozen() -> None:
    facts = attempt_facts()
    decision = decide_attempt_completion(facts)

    with pytest.raises(FrozenInstanceError):
        attempt_to_mutate(facts, "is_current_attempt")
    with pytest.raises(FrozenInstanceError):
        attempt_to_mutate(decision, "affects_current_instance")
    assert facts.is_current_attempt
    assert decision == AttemptTerminalDecision("succeeded", None, True)
