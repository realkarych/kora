from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from kora.protocols.runtime import (
    CAPABILITY_FLAGS,
    ExecutionDeadline,
    PlanExecutor,
    RuntimeCapabilities,
    ScopeKind,
)

CAPABILITIES: RuntimeCapabilities = RuntimeCapabilities(
    supports_resume=False,
    portable_handles=False,
    enforces_network_policy=False,
    isolates_tool_secrets=False,
    credential_proxy=False,
    protected_execution=False,
    plan_executors=frozenset({"runner"}),
    max_concurrent=4,
    default_stall_after=timedelta(minutes=10),
)
DEADLINE: ExecutionDeadline = ExecutionDeadline(
    at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
    scope_kind="run",
    scope_id="run-1",
    clock_uncertainty=timedelta(seconds=1),
    grace=timedelta(seconds=30),
)
PLUS_THREE: timezone = timezone(timedelta(hours=3))

CAPABILITY_CASES: list[tuple[str, object]] = [
    *((flag, 1) for flag in CAPABILITY_FLAGS),
    ("protected_execution", None),
    ("plan_executors", frozenset()),
    ("plan_executors", frozenset({"runner", "elsewhere"})),
    ("plan_executors", {"runner"}),
    ("max_concurrent", True),
    ("max_concurrent", 0),
    ("max_concurrent", 2.0),
    ("default_stall_after", timedelta(0)),
    ("default_stall_after", 600),
]
DEADLINE_CASES: list[tuple[str, object]] = [
    ("at", datetime(2026, 10, 21, 12, 0)),
    ("at", datetime(2026, 10, 21, 15, 0, tzinfo=PLUS_THREE)),
    ("scope_kind", "node"),
    ("scope_id", ""),
    ("scope_id", 1),
    ("clock_uncertainty", timedelta(microseconds=-1)),
    ("grace", timedelta(seconds=29)),
    ("grace", 30),
]


def test_runtime_types_accept_valid_values() -> None:
    executors = cast("frozenset[PlanExecutor]", frozenset({"runner", "worker_host"}))
    scope = cast("ScopeKind", "attempt")

    capabilities = replace(CAPABILITIES, plan_executors=executors, max_concurrent=None)
    deadline = replace(DEADLINE, scope_kind=scope, clock_uncertainty=timedelta(0))

    assert capabilities.max_concurrent is None
    assert deadline.scope_kind == "attempt"


INVALID_CASES: list[tuple[RuntimeCapabilities | ExecutionDeadline, str, object]] = [
    *((CAPABILITIES, field, value) for field, value in CAPABILITY_CASES),
    *((DEADLINE, field, value) for field, value in DEADLINE_CASES),
]


@pytest.mark.parametrize(("valid", "field", "value"), INVALID_CASES)
def test_runtime_types_reject_invalid_values(
    valid: RuntimeCapabilities | ExecutionDeadline, field: str, value: object
) -> None:
    changes = {field: value}

    with pytest.raises(ValueError, match=rf"^{field} "):
        replace(valid, **changes)
