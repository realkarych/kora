import asyncio
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import graph_version, namespace, price_table
from kora.engine.admission_preparation import (
    AdmissionPreparationRejected,
    admission_preparation_reservation,
    prepare_admission_snapshot,
)
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.engine.run_input_values import decode_run_input_value
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.admission_preparation_support import (
    LIMITS,
    observe_detached_checks,
    prepare,
)
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.test_admission_initialization import initializer, rates

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("distinct", [False, True])
async def test_admission_preparation_checks_complete_sources_after_release(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    distinct: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    cap_version = case.promotion.scope.version_id
    if distinct:
        ir = skeleton_ir()
        cast(JsonObject, ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
        cap_version = (await case.publish(ir)).graph_version_id.removeprefix("gv_")
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    with observe_detached_checks(database) as checks:
        prepared = await prepare(database, case)
    checkouts = ledger.checkouts
    parameters = decode_run_input_value(
        prepared.acquired.params, max_bytes=LIMITS.snapshot.inputs.params
    )
    parameters["consumer-local"] = True
    policy = prepared.policy.value()
    policy["consumer-local"] = True
    after = await case.promotion.snapshot(database)

    assert prepared.checked.scope == case.promotion.scope
    assert prepared.checked.pin == case.promotion.pin
    assert prepared.readiness.matches(case.promotion.scope, case.promotion.pin)
    assert prepared.caps.scope.version_id == cap_version
    assert prepared.cap_checked.scope == prepared.caps.scope
    assert (prepared.cap_checked is prepared.checked) is not distinct
    assert prepared.caps.nodes == (2 if distinct else None)
    assert prepared.acquired.params == b"{}"
    assert prepared.policy == case.policy
    assert "consumer-local" not in prepared.policy.value()
    assert prepared.price.id == case.price_id
    assert prepared.reservation == admission_preparation_reservation(LIMITS)
    assert checks.count("check_detached_execution_contract") == (2 if distinct else 1)
    assert checks.count("prepare_readiness") == checks.count("prepare_graph_caps") == 1
    assert checks.count("decode_run_input_value") == 1
    assert checkouts == 1
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_preparation_preserves_unicode_configuration(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    policy = NamespacePolicyConfiguration.prepare(
        case.promotion.scope.namespace_id,
        2,
        {
            "max_pending_runs": 1000,
            "metadata": {"text": "\u4ef7\u683c\U0001f600", "fraction": 0.125},
        },
    )
    configuration = initializer(
        database,
        (policy,),
        (
            PriceConfiguration(
                case.promotion.scope.namespace_id,
                case.promotion.clock.now(),
                {"\u6a21\u578b\U0001f600": rates("2")},
            ),
        ),
    )
    await configuration.initialize(api_capture.diagnostics)

    prepared = await prepare(database, case)

    assert prepared.policy == policy
    assert set(prepared.price.prices) == {"\u6a21\u578b\U0001f600"}
    assert prepared.price.id == configuration.prices[0].id
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["retained_units", "work_units"])
@pytest.mark.parametrize("short", [False, True])
async def test_admission_preparation_admits_its_complete_initial_allowance(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    field: str,
    short: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    reservation = admission_preparation_reservation(LIMITS)
    exact = replace(
        LIMITS,
        retained_units=reservation.retained_units,
        work_units=reservation.work_units,
    )
    limits = (
        replace(exact, retained_units=reservation.retained_units - int(short))
        if field == "retained_units"
        else replace(exact, work_units=reservation.work_units - int(short))
    )
    ledger = ConnectionLedger(database)

    if short:
        with pytest.raises(AdmissionPreparationRejected):
            await prepare(database, case, limits=limits)
    else:
        prepared = await prepare(database, case, limits=limits)
        assert prepared.checked.pin == case.promotion.pin

    assert ledger.checkouts == int(not short)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retained", "work"), [(7_000_000_000, 10**22), (10**12, 17_000_000_000_000_000)]
)
async def test_admission_preparation_reserves_the_second_checker_before_checkout(
    database: Database, retained: int, work: int
) -> None:
    limits = replace(LIMITS, retained_units=retained, work_units=work)
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionPreparationRejected):
        await prepare_admission_snapshot(
            database,
            "01J00000000000000000000000",
            limits,
            observed_at=START,
            periodic_recheck=True,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["graph", "policy", "price"])
async def test_admission_preparation_rejects_invalid_detached_content(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture, source: str
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    async with database.transaction() as writer:
        if source == "graph":
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == case.promotion.scope.version_id)
                .values(execution_contract_digest="f" * 64)
            )
        elif source == "policy":
            await writer.execute(
                update(namespace)
                .where(namespace.c.id == case.promotion.scope.namespace_id)
                .values(policy={"concurrency": {"max_running_attempts": False}})
            )
        else:
            await writer.execute(
                update(price_table)
                .where(price_table.c.id == case.price_id)
                .values(prices={"private-source": {}})
            )
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionPreparationRejected) as refused:
        await prepare(database, case)
    checkouts = ledger.checkouts
    after = await case.promotion.snapshot(database)

    assert str(refused.value) == "admission sources cannot be prepared"
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert checkouts == 1
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0
