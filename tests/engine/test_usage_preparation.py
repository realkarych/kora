from dataclasses import replace

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.graph_resource_owner_reads import acquire_graph_resource_owners
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.tables import graph_version, graph_version_resource_owner, worker
from kora.engine.attempt_usage import UsageAccess, UsageAccessRejected
from kora.engine.usage_preparation import read_usage_context
from tests.db.closing import ConnectionLedger
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import submit_usage, usage_case, usage_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [False, True])
async def test_usage_preparation_reads_the_exact_saved_contract(
    database: Database, resources: bool
) -> None:
    case = await usage_case(database, resources=resources)
    ledger = ConnectionLedger(database)

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )

    assert context == case.context
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expiring", "expired", "missing"])
async def test_usage_preparation_keeps_historical_resource_metadata(
    database: Database, state: str
) -> None:
    case = await usage_case(database, resources=True)
    async with database.transaction() as connection:
        await connection.execute(
            update(graph_version_resource_owner).values(payload_state=state)
        )

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )

    assert context == case.context


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ir", "resources"])
async def test_usage_preparation_checks_stale_grant_before_saved_material(
    database: Database, field: str
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        await connection.execute(update(graph_version).values({field: "x" * 1_000_000}))
    ledger = ConnectionLedger(database)

    with pytest.raises(UsageAccessRejected) as stale:
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            0,
            case.principal,
            case.clock,
            LIMITS,
        )
    with pytest.raises(GraphValueRejected):
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            1,
            case.principal,
            case.clock,
            LIMITS,
        )

    assert stale.value.reason is UsageAccess.STALE_GRANT
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [False, True])
async def test_usage_preparation_charges_resources_and_owners_together(
    database: Database, resources: bool
) -> None:
    case = await usage_case(database, resources=resources)
    async with database.read_transaction() as connection:
        payload = await connection.scalar(
            select(
                graph_resource_binary_projection(
                    database.dialect,
                    graph_version.c.resources,
                )
            )
        )
        assert payload is not None
        owners = await acquire_graph_resource_owners(
            connection,
            case.context.graph_version_id,
            max_text_bytes=100_000,
        )
        width = len(payload) + sum(
            len(value) for row in owners.rows for value in row if type(value) is bytes
        )
    exact = replace(LIMITS, physical_input_bytes=width)

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        exact,
    )
    with pytest.raises(GraphValueRejected) as overflow:
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            1,
            case.principal,
            case.clock,
            replace(exact, physical_input_bytes=width - 1),
        )

    assert context == case.context
    assert overflow.value.reason is GraphValueFailure.BUDGET


@pytest.mark.asyncio
async def test_usage_preparation_does_not_preserve_stale_write_authority(
    database: Database,
) -> None:
    case = await usage_case(database)
    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )
    async with database.transaction() as connection:
        await connection.execute(update(worker).values(incarnation_id="replacement"))
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, replace(case, context=context), (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.STALE_GRANT
    assert after == before
