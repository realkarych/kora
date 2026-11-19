from datetime import timedelta
from typing import cast

import pytest

from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.locks import COMPONENT
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.seed import START
from tests.engine.claim_support import (
    UNCERTAINTY,
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_zero_timeout_returns_learnable_expired_first_grant(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
    node["limits"] = {"timeout": "0ms"}
    case = await claim_case(database, evaluator, ir)

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "zero-timeout-grant",
        case.clock.now,
    ) as capture:
        first = await case.claim(database)
        records = capture.finish()
    saved = await state(database)
    replay = await case.claim(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert first.delivery is not None
    assert replay.delivery is not None
    assert replay.delivery.encode() == first.delivery.encode()
    assert first.delivery.system.permitted_actions == ("seal_request",)
    deadline = first.delivery.system.deadline.value
    assert deadline is not None
    assert deadline.at == "2026-10-20T12:00:00.000000Z"
    assert deadline.clock_uncertainty_us == str(
        UNCERTAINTY // timedelta(microseconds=1)
    )
    assert saved["attempt"][0]["deadline_expired_at"] == START
    assert saved["attempt"][0]["recovery_required"] is True
    assert saved["attempt"][0]["desired_state"] == "cancel"
    assert saved["run"][0]["needs_scheduling"] is True
    assert saved["run"][0]["deadline_expired_at"] is None
    assert [
        row["action"] for row in records if row["event"] == "engine.expiry.prepared"
    ] == ["attempt_expiry_intent"]
    assert case.binding.attempt_id not in repr(records)
