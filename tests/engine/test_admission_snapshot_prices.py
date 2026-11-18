from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import Database
from kora.db.tables import price_table
from kora.engine.price_initialization import PriceConfiguration
from kora.ir.cost import PriceRevision
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.engine.admission_snapshot_support import acquire, snapshot_case
from tests.engine.test_admission_initialization import initializer, rates

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["default", "future", "namespace", "empty"])
async def test_admission_snapshot_selects_whole_effective_price_revision(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    selected: str,
) -> None:
    case = await snapshot_case(database, preparer, api_capture, namespace_price=False)
    configuration = PriceConfiguration(
        case.promotion.scope.namespace_id,
        case.promotion.clock.now()
        + timedelta(seconds=1 if selected == "future" else 0),
        {} if selected == "empty" else {"namespace-model": rates("2")},
    )
    initialization = initializer(database, prices=(configuration,))
    if selected != "default":
        await initialization.initialize(api_capture.diagnostics)
    ledger = ConnectionLedger(database)

    acquired = await acquire(database, case)
    assert ledger.outstanding == database.units_in_flight() == 0
    prices = decode_claim_source(
        acquired.price.prices, acquired.graph.physical.encoding
    )

    scoped = selected in ("namespace", "empty")
    assert acquired.price_table_id == (
        initialization.prices[0].id if scoped else case.price_id
    )
    assert acquired.price.namespace_id == (
        case.promotion.scope.namespace_id if scoped else None
    )
    assert isinstance(prices, dict)
    assert set(prices) == (
        set() if selected == "empty" else {"namespace-model"} if scoped else {"model"}
    )
    assert ledger.checkouts == 1


@pytest.mark.asyncio
async def test_admission_snapshot_never_falls_back_from_malformed_selected_price(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    async with database.transaction() as writer:
        await writer.execute(
            update(price_table)
            .where(price_table.c.id == case.price_id)
            .values(prices={"private-malformed-price": {}})
        )
    ledger = ConnectionLedger(database)

    acquired = await acquire(database, case)
    assert ledger.outstanding == database.units_in_flight() == 0
    encoding = acquired.graph.physical.encoding
    with pytest.raises(ValueError, match="invalid accounting value"):
        PriceRevision.model_validate(
            {
                "id": acquired.price_table_id,
                "namespace_id": acquired.price.namespace_id,
                "effective_from": acquired.price.effective_from.decode(encoding),
                "version": 1,
                "normalization_version": 1,
                "prices": decode_claim_source(acquired.price.prices, encoding),
            }
        )

    assert acquired.price_table_id == case.price_id
