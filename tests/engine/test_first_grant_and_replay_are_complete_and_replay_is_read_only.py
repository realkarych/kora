from datetime import timedelta
from typing import cast

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_first_grant_and_replay_are_complete_and_replay_is_read_only(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)

    first = await case.claim(database)
    before_replay = await state(database)
    case.clock.advance(1)
    replay = await case.claim(database)
    after_replay = await state(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert replay.outcome is ClaimOutcome.REPLAYED
    assert first.delivery is not None
    assert replay.delivery is not None
    assert first.delivery.encode() == replay.delivery.encode()
    assert before_replay == after_replay
    system = first.delivery.system
    assert system.permitted_actions == ("seal_request",)
    assert system.lease_epoch == 1
    assert system.lease_until == START + timedelta(seconds=120)
    assert system.acknowledged_segment_offset == 0
    assert system.current_grant.process == case.registration.process
    assert system.grant_history.count == 1
    document = ObjectReader(first.delivery.encode().decode()).object()
    claim = cast(JsonObject, document["claim"])
    assert claim["price_table_id"] == case.binding.price_table_id
    assert claim["resolved_config"] is not None
    assert claim["execution_context"] is not None
    assert claim["execution_policy"] is not None
    claimed = [row for row in after_replay["event"] if row["kind"] == "attempt.claimed"]
    assert len(claimed) == 1
    assert claimed[0]["actor"] == {
        "kind": "service",
        "owner": case.principal.owner,
        "token_id": case.principal.token_id,
    }
    assert after_replay["attempt"][0]["started_at"] == START
