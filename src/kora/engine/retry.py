from kora.engine.retry_models import (
    RUNTIME_RETRY_CLASSES,
    NodeRetryDecision,
    NodeRetryDenied,
    NodeRetryFacts,
    NodeRetryPolicyAllows,
    PredicateFailure,
    PredicatePending,
    PredicateResult,
    RetryPredicateRequired,
    RuntimeRetryPolicyRequired,
)
from kora.ir.node import ErrorClass

NEVER_RETRY_CLASSES: frozenset[ErrorClass] = frozenset(
    (
        ErrorClass.CONFIG_ERROR,
        ErrorClass.CANCELLED,
        ErrorClass.CANDIDATE_MOVED,
        ErrorClass.INTEGRITY_ERROR,
    )
)


def decide_node_retry_eligibility(facts: NodeRetryFacts) -> NodeRetryDecision:
    failure = facts.failure_class
    if failure in RUNTIME_RETRY_CLASSES:
        return RuntimeRetryPolicyRequired(failure)
    if failure in NEVER_RETRY_CLASSES:
        return NodeRetryDenied(failure, "never_retryable")
    policy = facts.policy
    if failure in policy.non_retryable:
        return NodeRetryDenied(failure, "non_retryable")
    if failure not in policy.retry_on:
        return NodeRetryDenied(failure, "not_selected")
    if facts.consumed_policy_attempts >= policy.max_attempts:
        return NodeRetryDenied(failure, "attempts_exhausted")
    predicate = facts.predicate
    if isinstance(predicate, PredicatePending):
        return RetryPredicateRequired(failure)
    if isinstance(predicate, PredicateFailure):
        return NodeRetryDenied(failure, "predicate_error", predicate.kind)
    if isinstance(predicate, PredicateResult) and not predicate.value:
        return NodeRetryDenied(failure, "predicate_false")
    return NodeRetryPolicyAllows(failure)
