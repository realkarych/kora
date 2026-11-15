from typing import cast

import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.graph_value_json import ObjectReader
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_execution_contract import (
    MEMBERS,
    check_detached_execution_contract,
)
from kora.engine.graph_output_values import GraphOutputContract
from kora.engine.graph_outputs import (
    build_graph_output_contract,
    read_graph_output_contract,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceScope,
    build_graph_resource_references,
)
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.start_records import prepare_manual_start_request
from kora.engine.start_replay import ReplayOutcome, read_manual_start_candidate
from kora.protocols.authentication import Principal
from tests.api.test_graph_execution_preparation import MARKER, OWNER_MARKER
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder, stored_graph_version
from tests.engine.graph_output_cases import LIMIT
from tests.engine.saved_output_history import historical_outputs
from tests.engine.start_support import KEY, PRIVATE, replay_scenario
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_graph_resource_references import ROOT, content
from tests.engine.test_start_replay import observed_statements
from tests.support.output_bindings import publication_binding


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(4))
async def test_saved_output_reader_dispatches_immutable_policy(
    database: Database, index: int
) -> None:
    original = historical_outputs().leaves[index]
    encoded = prepare_graph_values(
        ObjectReader(original.ir).object(),
        ObjectReader(original.contract).object(),
        max_encoded_bytes=LIMIT,
    )
    ledger = ConnectionLedger(database)
    row, foreign = await stored_graph_version(
        database,
        encoded,
        execution_contract_digest=original.digest,
    )
    assert row is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    saved = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    assert isinstance(saved, DecodedGraphValues)

    actual = read_graph_output_contract(
        cast(str, row.ir),
        original.type_contracts,
        original.path,
        saved.execution_contract,
        row.execution_contract_digest,
        max_encoded_bytes=LIMIT,
    )
    current = build_graph_output_contract(
        original.ir,
        original.type_contracts,
        original.path,
        binding=publication_binding(),
        max_encoded_bytes=LIMIT,
    )
    await database.close()

    assert actual == GraphOutputContract(original.contract, original.digest)
    assert row.ir == original.ir
    assert row.execution_contract == original.contract
    assert isinstance(current, GraphOutputContract)
    assert current.digest != original.digest
    assert foreign is None
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_saved_output_whole_history_reads_after_database_release(
    database: Database,
) -> None:
    original = historical_outputs().whole
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seed.new_id())
    owners = (
        GraphResourceOwner(
            scope, ROOT, "é.sh", content(store="second-store", incarnation=OWNER_MARKER)
        ),
        GraphResourceOwner(scope, ROOT, "z.sh", content(incarnation=MARKER)),
    )
    physical = build_graph_resource_references(
        original.ir,
        scope,
        owners,
        limits=LIMITS.resources,
    )
    async with database.transaction() as connection:
        await Seeder(connection).graph_version(
            graph_id,
            ir=original.ir,
            execution_contract=original.contract,
            value_encoding_version=1,
            version_id=scope.version_id,
            ir_digest=original.ir_digest,
            execution_contract_digest=original.execution_contract_digest,
            deployment_digest=original.deployment_digest,
            resources=ObjectReader(physical.text).object(),
        )
    async with database.read_snapshot() as connection:
        acquired = await acquire_graph_version(
            connection,
            namespace_id,
            graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        row = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert acquired is not None
    assert row is not None
    checkouts = ledger.checkouts
    assert ledger.outstanding == database.units_in_flight() == 0
    pin = GraphVersionPin(
        graph_id, scope.version_id, original.execution_contract_digest
    )

    checked = check_detached_execution_contract(
        acquired,
        pin,
        scope,
        row.resources,
        owners,
        limits=LIMITS,
    )
    after_checkouts = ledger.checkouts
    await database.close()

    assert checked.canonical_ir == original.ir
    assert checked.canonical_contract == original.contract
    assert checked.parameter_evaluation is None
    assert checked.pin == pin
    assert row.ir_digest == original.ir_digest
    assert row.deployment_digest == original.deployment_digest
    assert tuple(ObjectReader(checked.canonical_contract).object()) == MEMBERS
    assert after_checkouts == checkouts
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["accepted", "skipped", "changed", "denied"])
async def test_schema_policy_history_and_replay_are_immutable(
    database: Database,
    ending: str,
) -> None:
    original = historical_outputs().whole
    scenario = await replay_scenario(database, skipped=ending == "skipped")
    identity = scenario.identity
    principal = scenario.principal
    if ending == "denied":
        principal = Principal.model_validate(
            principal.model_dump() | {"namespaces": ()}
        )
    request = prepare_manual_start_request(
        identity.namespace_id,
        identity.graph_id,
        {} if ending == "changed" else {"private": PRIVATE},
    )
    async with database.transaction() as connection:
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == scenario.version_id)
            .values(
                ir=original.ir,
                execution_contract=original.contract,
                value_encoding_version=1,
                ir_digest=original.ir_digest,
                execution_contract_digest=original.execution_contract_digest,
                deployment_digest=original.deployment_digest,
            )
        )
    before = await scenario.snapshot()

    async with database.read_snapshot() as connection:
        async with observed_statements(connection) as statements:
            result = await read_manual_start_candidate(
                connection, principal, request, KEY
            )
    after = await scenario.snapshot()

    assert result.version_id is None
    assert result.replay.outcome is (
        ReplayOutcome.REFUSED
        if ending == "denied"
        else ReplayOutcome.MISMATCHED
        if ending == "changed"
        else ReplayOutcome.MATCHED
    )
    if ending in {"accepted", "skipped"}:
        assert result.replay.response == scenario.receipt.response
    assert len(statements) == (0 if ending == "denied" else 1)
    assert all("FROM inbound_event" in statement for statement in statements)
    assert after == before
