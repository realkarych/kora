from dataclasses import replace

import pytest

from kora.db.connection import Database
from kora.engine.attempt_preparation_budget import (
    admit_attempt_preparation,
    attempt_preparation_reservation,
)
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.closing import ConnectionLedger
from tests.engine.attempt_admission_support import PREPARATION_LIMITS
from tests.engine.optional_input_support import optional_admission_case


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["retained", "work", "preflight"])
async def test_optional_input_preparation_charges_generated_maps_and_strings(
    database: Database, boundary: str
) -> None:
    case = await optional_admission_case(database)
    node = case.readiness.nodes[1]
    reservation = attempt_preparation_reservation(
        case.checked, PREPARATION_LIMITS, node.inputs
    )
    root = attempt_preparation_reservation(case.checked, PREPARATION_LIMITS)
    exact = replace(
        PREPARATION_LIMITS,
        retained_units=reservation.retained_units,
        work_units=reservation.work_units,
    )
    insufficient = (
        replace(exact, retained_units=exact.retained_units - 1)
        if boundary == "retained"
        else replace(
            exact,
            work_units=exact.work_units - 1
            if boundary == "work"
            else 32 * (len(node.inputs) + 1) ** 2 - 1,
        )
    )
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimWireRejected):
        admit_attempt_preparation(case.checked, insufficient, node.inputs)
    admitted = admit_attempt_preparation(case.checked, exact, node.inputs)

    assert admitted == reservation
    assert reservation.retained_units > root.retained_units
    assert reservation.work_units > root.work_units
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0
