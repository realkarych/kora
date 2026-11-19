from dataclasses import replace
from datetime import timedelta

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.worker_registration import register_worker
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_retained_incarnation_replays_its_original_process_after_registration(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    first = await case.claim(database)
    next_registration = replace(
        case.registration,
        incarnation_id="new-incarnation",
        process=case.registration.process.model_copy(update={"pid": 124}),
    )
    async with database.transaction() as writer:
        await register_worker(
            writer,
            next_registration,
            case.principal,
            portable=False,
            now=START + timedelta(seconds=1),
            event_id=case.identifiers.new(),
        )
    before = await state(database)

    replay = await case.claim(database)
    after = await state(database)

    assert replay.outcome is ClaimOutcome.REPLAYED
    assert replay.delivery is not None
    assert first.delivery is not None
    assert replay.delivery.encode() == first.delivery.encode()
    assert before == after
