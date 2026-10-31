from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import MISSING

from kora.cel_eval.errors import ErrorKind
from kora.engine.retry import decide_node_retry_eligibility
from kora.engine.retry_models import (
    NodeRetryDenied,
    NodeRetryFacts,
    NodeRetryPolicyAllows,
    NodeRetryPolicyFacts,
    NoPredicate,
    PredicateFailure,
    PredicatePending,
    PredicateResult,
    RetryPredicate,
    RetryPredicateRequired,
    RuntimeRetryPolicyRequired,
)
from kora.ir.node import ErrorClass


def retry_facts(predicate: RetryPredicate | None = None) -> NodeRetryFacts:
    policy = NodeRetryPolicyFacts(
        3, frozenset((ErrorClass.HARNESS_ERROR,)), frozenset(), predicate is not None
    )
    return NodeRetryFacts(
        ErrorClass.HARNESS_ERROR,
        policy,
        1,
        NoPredicate() if predicate is None else predicate,
    )


@pytest.mark.parametrize(
    "failure",
    [
        ErrorClass.CONFIG_ERROR,
        ErrorClass.CANCELLED,
        ErrorClass.CANDIDATE_MOVED,
        ErrorClass.INTEGRITY_ERROR,
    ],
)
def test_never_retry_classes_cannot_be_enabled(failure: ErrorClass) -> None:
    original = retry_facts(PredicateFailure(ErrorKind.INTERNAL_FAULT))
    policy = replace(original.policy, retry_on=frozenset((failure,)))
    facts = replace(original, failure_class=failure, policy=policy)

    result = decide_node_retry_eligibility(facts)

    assert result == NodeRetryDenied(failure, "never_retryable")


@pytest.mark.parametrize(
    "failure", [ErrorClass.INFRASTRUCTURE, ErrorClass.PROVIDER_LIMITED]
)
@pytest.mark.parametrize("count", [0, 1, 100])
@pytest.mark.parametrize(
    "predicate",
    [PredicatePending(), PredicateResult(False), PredicateFailure(ErrorKind.OVERFLOW)],
)
def test_runtime_classes_require_their_own_policy(
    failure: ErrorClass, count: int, predicate: RetryPredicate
) -> None:
    original = retry_facts(predicate)
    policy = replace(
        original.policy,
        retry_on=frozenset((failure,)),
        non_retryable=frozenset((failure,)),
    )
    facts = replace(
        original, failure_class=failure, policy=policy, consumed_policy_attempts=count
    )

    result = decide_node_retry_eligibility(facts)

    assert result == RuntimeRetryPolicyRequired(failure)
    assert facts.consumed_policy_attempts == count


@pytest.mark.parametrize(
    "predicate",
    [PredicatePending(), PredicateResult(True), PredicateFailure(ErrorKind.OVERFLOW)],
)
@pytest.mark.parametrize("denied", [False, True])
def test_node_retry_class_gates_precede_predicate(
    predicate: RetryPredicate, denied: bool
) -> None:
    original = retry_facts(predicate)
    failure = original.failure_class
    policy = replace(
        original.policy,
        retry_on=frozenset((failure,)) if denied else frozenset(),
        non_retryable=frozenset((failure,)) if denied else frozenset(),
    )
    facts = replace(original, policy=policy)

    result = decide_node_retry_eligibility(facts)

    assert result == NodeRetryDenied(
        failure, "non_retryable" if denied else "not_selected"
    )


@pytest.mark.parametrize(
    ("maximum", "count"),
    [(1, 1), (3, 2), (3, 3), (3, 4), (100, 99), (100, 100), (100, 101)],
)
def test_node_retry_count_boundaries_precede_predicate(
    maximum: int, count: int
) -> None:
    original = retry_facts(PredicateFailure(ErrorKind.PROJECTION_UNAVAILABLE))
    policy = replace(original.policy, max_attempts=maximum)
    facts = replace(original, policy=policy, consumed_policy_attempts=count)
    expected = (
        NodeRetryDenied(original.failure_class, "attempts_exhausted")
        if count >= maximum
        else NodeRetryDenied(
            original.failure_class, "predicate_error", ErrorKind.PROJECTION_UNAVAILABLE
        )
    )

    result = decide_node_retry_eligibility(facts)

    assert result == expected
    assert facts.consumed_policy_attempts == count


def test_configured_predicate_is_required_only_after_other_gates() -> None:
    facts = retry_facts(PredicatePending())

    result = decide_node_retry_eligibility(facts)

    assert result == RetryPredicateRequired(ErrorClass.HARNESS_ERROR)


def test_absent_predicate_differs_from_pending_evaluation() -> None:
    absent = retry_facts()
    pending = retry_facts(PredicatePending())

    absent_result = decide_node_retry_eligibility(absent)
    pending_result = decide_node_retry_eligibility(pending)

    assert absent_result == NodeRetryPolicyAllows(ErrorClass.HARNESS_ERROR)
    assert pending_result == RetryPredicateRequired(ErrorClass.HARNESS_ERROR)


@pytest.mark.parametrize("value", [False, True])
def test_predicate_result_requires_exact_boolean(value: bool) -> None:
    facts = retry_facts(PredicateResult(value))

    result = decide_node_retry_eligibility(facts)

    assert result == (
        NodeRetryPolicyAllows(ErrorClass.HARNESS_ERROR)
        if value
        else NodeRetryDenied(ErrorClass.HARNESS_ERROR, "predicate_false")
    )


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_predicate_failure_preserves_original_class(kind: ErrorKind) -> None:
    facts = retry_facts(PredicateFailure(kind))

    result = decide_node_retry_eligibility(facts)

    assert result == NodeRetryDenied(ErrorClass.HARNESS_ERROR, "predicate_error", kind)


@pytest.mark.parametrize(
    "failure",
    [
        ErrorClass.HARNESS_ERROR,
        ErrorClass.COMMAND_FAILED,
        ErrorClass.TIMEOUT,
        ErrorClass.EVENTS_EXHAUSTED,
        ErrorClass.BUDGET_EXHAUSTED,
        ErrorClass.ARTIFACT_INVALID,
        ErrorClass.ATTEMPT_FAILED,
        ErrorClass.APPROVAL_REJECTED,
        ErrorClass.APPROVAL_EXPIRED,
        ErrorClass.WAIT_EXPIRED,
        ErrorClass.STALLED,
        ErrorClass.PROTECTED_PATH_MODIFIED,
        ErrorClass.CREDENTIAL_LEAK,
    ],
)
def test_ordinary_classes_use_the_saved_allow_membership(failure: ErrorClass) -> None:
    original = retry_facts()
    policy = replace(original.policy, retry_on=frozenset((failure,)))
    facts = replace(original, failure_class=failure, policy=policy)

    result = decide_node_retry_eligibility(facts)

    assert result == NodeRetryPolicyAllows(failure)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_attempts": MISSING},
        {"max_attempts": None},
        {"max_attempts": True},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"retry_on": None},
        {"retry_on": frozenset(("harness_error",))},
        {"retry_on": {ErrorClass.HARNESS_ERROR}},
        {"non_retryable": frozenset(("content marker",))},
        {"predicate_configured": 1},
    ],
)
def test_retry_policy_refuses_incomplete_or_wrong_values(
    changes: dict[str, object],
) -> None:
    policy = retry_facts().policy

    with pytest.raises(ValueError, match="^retry facts are invalid$"):
        replace(policy, **changes)

    assert policy.max_attempts == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"failure_class": "harness_error"},
        {"policy": None},
        {"consumed_policy_attempts": True},
        {"consumed_policy_attempts": -1},
        {"consumed_policy_attempts": 0},
        {"predicate": None},
        {"predicate": True},
        {"predicate": PredicatePending()},
    ],
)
def test_retry_facts_refuse_inconsistent_or_incomplete_values(
    changes: dict[str, object],
) -> None:
    facts = retry_facts()

    with pytest.raises(ValueError, match="^retry facts are invalid$"):
        replace(facts, **changes)

    assert facts.consumed_policy_attempts == 1


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_predicate_result_refuses_truthy_and_missing_values(value: object) -> None:
    predicate = PredicateResult(True)

    with pytest.raises(ValueError, match="^retry facts are invalid$"):
        replace(predicate, value=value)

    assert predicate.value is True


def test_missing_predicate_result_cannot_claim_absence() -> None:
    facts = retry_facts(PredicatePending())

    with pytest.raises(ValueError, match="^retry facts are invalid$"):
        replace(facts, predicate=NoPredicate())

    assert isinstance(facts.predicate, PredicatePending)


def test_predicate_failure_requires_closed_error_kind() -> None:
    predicate = PredicateFailure(ErrorKind.INTERNAL_FAULT)

    with pytest.raises(ValueError, match="^retry facts are invalid$"):
        replace(predicate, kind="internal_fault")

    assert predicate.kind is ErrorKind.INTERNAL_FAULT


def test_retry_values_are_frozen() -> None:
    facts = retry_facts()
    values: tuple[object, ...] = (
        facts,
        facts.policy,
        NoPredicate(),
        PredicatePending(),
        PredicateResult(True),
        PredicateFailure(ErrorKind.OVERFLOW),
        NodeRetryDenied(ErrorClass.HARNESS_ERROR, "non_retryable"),
        RetryPredicateRequired(ErrorClass.HARNESS_ERROR),
        NodeRetryPolicyAllows(ErrorClass.HARNESS_ERROR),
        RuntimeRetryPolicyRequired(ErrorClass.INFRASTRUCTURE),
    )

    for value in values:
        with pytest.raises(FrozenInstanceError):
            setattr(value, "unexpected", True)

    assert all(not hasattr(value, "unexpected") for value in values)
