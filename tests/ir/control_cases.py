from pydantic import BaseModel

from kora.ir import (
    BranchCase,
    BranchConfig,
    ErrorClass,
    FanOutConfig,
    Retry,
    SubgraphNodeConfig,
)

CASE: dict[str, object] = {"when": "true", "to": ["target"]}
MINIMAL: dict[type[BaseModel], dict[str, object]] = {
    BranchCase: CASE,
    BranchConfig: {"cases": [CASE]},
    FanOutConfig: {"body": "body", "over": "items"},
    SubgraphNodeConfig: {"body": "body"},
    Retry: {},
}
POPULATED: dict[type[BaseModel], dict[str, object]] = {
    BranchCase: {
        "when": "inputs.ready",
        "to": ["a", "$fail_attempt", "a"],
        "reason": "'failed'",
    },
    BranchConfig: {
        "cases": [CASE],
        "else": ["$fail_attempt", "b", "b"],
        "else_reason": "'no match'",
    },
    FanOutConfig: {
        "body": "body",
        "over": "items",
        "max_parallel": 1,
        "skip_failures": False,
    },
    SubgraphNodeConfig: {"body": "body", "x-label": {"ready": False}},
    Retry: {
        "max_attempts": 100,
        "initial_interval": "0ms",
        "max_interval": "10d",
        "backoff_coefficient": 1.5,
        "retry_on": [item.value for item in ErrorClass],
        "non_retryable": [item.value for item in reversed(ErrorClass)],
        "predicate": "outputs.result.ok",
        "strategy": "fresh_with_feedback",
    },
}
