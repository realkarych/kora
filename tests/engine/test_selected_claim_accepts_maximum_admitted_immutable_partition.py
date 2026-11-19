from dataclasses import replace
from typing import cast

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import (
    MAX_CLAIM_BYTES,
    MAX_IMMUTABLE_BYTES,
    ClaimPartition,
    prepare_claim_part,
)
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import (
    claim_case,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_selected_claim_accepts_maximum_admitted_immutable_partition(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    immutable = ObjectReader(
        prepared.snapshot.material.immutable.payload.decode()
    ).object()
    config = cast(JsonObject, immutable["resolved_config"])
    config["prompt"] = ""
    base = prepare_claim_part(ClaimPartition.IMMUTABLE, immutable)
    config["prompt"] = "a" * (MAX_IMMUTABLE_BYTES - len(base.payload))
    largest = prepare_claim_part(ClaimPartition.IMMUTABLE, immutable)
    selected = replace(
        prepared,
        snapshot=replace(
            prepared.snapshot,
            material=replace(prepared.snapshot.material, immutable=largest),
        ),
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        decision = await case.apply(writer, selected)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert decision.delivery is not None
    response = decision.delivery.encode()

    assert decision.outcome is ClaimOutcome.GRANTED
    assert len(largest.payload) == MAX_IMMUTABLE_BYTES
    assert len(response) <= MAX_CLAIM_BYTES
    assert b'"prompt":"' + b"a" * 256 in response
