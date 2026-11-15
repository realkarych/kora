from dataclasses import replace

import pytest

from kora.api.run_trigger_limits import START_PREPARATION
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueRejected
from kora.engine.start_acceptance import (
    StartCandidate,
    acquire_manual_start,
    select_manual_start,
)
from kora.engine.start_facts import (
    ManualStartFacts,
    SelectedStartVersion,
    read_manual_start_facts,
)
from tests.api.deployment_cases import SELECTION, evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.start_cases import KEY, deploy, request_for

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_trigger_admission_precedes_acquisition_and_checks_are_detached(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    candidate = await select_manual_start(database, case.user, request, KEY)
    assert isinstance(candidate, StartCandidate)
    ledger = ConnectionLedger(database)
    before = ledger.checkouts

    for field in ("retained", "work"):
        short = replace(
            START_PREPARATION, **{field: getattr(START_PREPARATION, field) - 1}
        )
        with pytest.raises(GraphValueRejected):
            await acquire_manual_start(database, request, candidate, limits=short)
    refused_checkouts = ledger.checkouts
    acquired = await acquire_manual_start(
        database, request, candidate, limits=START_PREPARATION
    )
    assert isinstance(acquired, SelectedStartVersion)
    detached_checkouts = ledger.checkouts
    facts = read_manual_start_facts(
        acquired, request, SELECTION, key=KEY, limits=START_PREPARATION
    )

    assert refused_checkouts == before
    assert ledger.checkouts == detached_checkouts
    assert ledger.outstanding == database.units_in_flight() == 0
    assert isinstance(facts, ManualStartFacts)
    assert facts.projection == b"{}"
    assert facts.belongs_to(request, KEY)
    assert not facts.belongs_to(request, "another-key")


@pytest.mark.parametrize("field", ["native_bytes", "owner_text_bytes"])
def test_trigger_reservation_cannot_omit_acquisition_owners(field: str) -> None:
    larger = replace(
        START_PREPARATION.snapshot, **{field: START_PREPARATION.retained * 2}
    )
    limits = replace(START_PREPARATION, snapshot=larger)

    with pytest.raises(GraphValueRejected):
        limits.admit()
