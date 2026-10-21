from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Literal, cast

PlanExecutor = Literal["runner", "worker_host"]
ScopeKind = Literal["run", "subgraph", "attempt"]

PLAN_EXECUTORS: frozenset[str] = frozenset({"runner", "worker_host"})
SCOPE_KINDS: frozenset[str] = frozenset({"run", "subgraph", "attempt"})
DEADLINE_GRACE: timedelta = timedelta(seconds=30)
CAPABILITY_FLAGS: tuple[str, ...] = (
    "supports_resume",
    "portable_handles",
    "enforces_network_policy",
    "isolates_tool_secrets",
    "credential_proxy",
    "protected_execution",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def is_bool(value: object) -> bool:
    return isinstance(value, bool)


def is_positive_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_executor_set(value: object) -> bool:
    if not isinstance(value, frozenset):
        return False
    items = cast("frozenset[object]", value)
    return len(items) > 0 and all(item in PLAN_EXECUTORS for item in items)


def is_duration(value: object, minimum: timedelta, inclusive: bool) -> bool:
    if not isinstance(value, timedelta):
        return False
    return value >= minimum if inclusive else value > minimum


def is_utc(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() == timedelta(0)


def is_scope_kind(value: object) -> bool:
    return isinstance(value, str) and value in SCOPE_KINDS


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


@dataclass(frozen=True)
class RuntimeCapabilities:
    supports_resume: bool
    portable_handles: bool
    enforces_network_policy: bool
    isolates_tool_secrets: bool
    credential_proxy: bool
    protected_execution: bool
    plan_executors: frozenset[PlanExecutor]
    max_concurrent: int | None
    default_stall_after: timedelta

    def __post_init__(self) -> None:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        for flag in CAPABILITY_FLAGS:
            require(is_bool(values[flag]), f"{flag} is a bool")
        require(
            is_executor_set(self.plan_executors),
            "plan_executors is a non-empty frozenset of runner and worker_host",
        )
        require(
            self.max_concurrent is None or is_positive_count(self.max_concurrent),
            "max_concurrent is None or a positive int",
        )
        require(
            is_duration(self.default_stall_after, timedelta(0), inclusive=False),
            "default_stall_after is a positive duration",
        )


@dataclass(frozen=True)
class ExecutionDeadline:
    at: datetime
    scope_kind: ScopeKind
    scope_id: str
    clock_uncertainty: timedelta
    grace: timedelta

    def __post_init__(self) -> None:
        require(is_utc(self.at), "at is a UTC datetime")
        require(
            is_scope_kind(self.scope_kind), "scope_kind is run, subgraph or attempt"
        )
        require(is_identifier(self.scope_id), "scope_id is a non-empty string")
        require(
            is_duration(self.clock_uncertainty, timedelta(0), inclusive=True),
            "clock_uncertainty is a non-negative duration",
        )
        require(self.grace == DEADLINE_GRACE, "grace is 30 seconds")
