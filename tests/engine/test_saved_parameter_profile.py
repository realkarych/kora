from dataclasses import replace
from typing import cast

import pytest

from kora.api.graph_execution_preparation import (
    PreparedExecutionContract,
    prepare_execution_contract,
)
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueRejected, ObjectReader
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.reads import graph_version_by_number
from kora.engine.graph_execution_contract import (
    MEMBERS,
    PARAMETER_MEMBERS,
    check_detached_execution_contract,
)
from kora.engine.graph_parameter_evaluation import (
    parameter_request_reservation,
    validate_graph_parameter_value,
)
from kora.engine.graph_resource_references import GraphResourceOwner, GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.start_records import prepare_manual_start_request
from kora.ir.json import JsonObject
from kora.ir.standard_schema_policy import SchemaDomain
from tests.api.deployment_cases import evaluator
from tests.api.test_graph_execution_preparation import LIMITS, detached, source
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.test_graph_execution_contract import prepared_case, transport
from tests.engine.test_graph_resource_references import SCOPE
from tests.ir.standard_schema_cases import binding
from tests.support.output_bindings import publication_binding
from tests.support.parameter_bindings import parameter_binding

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_parameter_profile_round_trip_and_handoff(
    database: Database, evaluator: Evaluator
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seed.new_id())
    ir, old_owners = source(ordered=True)
    owners = tuple(
        GraphResourceOwner(scope, owner.path, owner.name, owner.reference)
        for owner in old_owners
    )
    prepared = await prepare_execution_contract(
        ir,
        scope,
        owners,
        evaluator,
        binding=publication_binding(),
        parameter_binding=parameter_binding(),
        limits=LIMITS,
    )
    assert isinstance(prepared, PreparedExecutionContract)
    hashes = prepared.digests
    async with database.transaction() as connection:
        await Seeder(connection).graph_version(
            graph_id,
            version_id=scope.version_id,
            ir=prepared.canonical_ir,
            execution_contract=prepared.canonical_contract,
            value_encoding_version=1,
            ir_digest=hashes.ir_digest,
            execution_contract_digest=hashes.execution_contract_digest,
            deployment_digest=hashes.deployment_digest,
            resources=ObjectReader(prepared.resources.text).object(),
        )
    async with database.read_snapshot() as connection:
        acquired = await acquire_graph_version(
            connection,
            namespace_id,
            graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.reader.identity.max_encoded_bytes,
        )
        row = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert acquired is not None
    assert row is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    checkouts = ledger.checkouts
    pin = GraphVersionPin(graph_id, scope.version_id, hashes.execution_contract_digest)

    checked = check_detached_execution_contract(
        acquired, pin, scope, row.resources, owners, limits=LIMITS.reader
    )
    profile = checked.parameter_evaluation
    assert profile is not None
    outcomes: list[str] = []
    for value in (True, 2):
        request = prepare_manual_start_request(
            namespace_id, graph_id, {"enabled": value}
        )
        original = request.identity, request.parameter_bytes()
        retained, work = parameter_request_reservation(request, profile)
        result = validate_graph_parameter_value(
            request, profile, max_retained_bytes=retained, max_work=work
        )
        outcomes.append(result.outcome)
        assert (request.identity, request.parameter_bytes()) == original
    await database.close()

    assert outcomes == ["valid", "invalid"]
    assert checked.pin == pin
    assert checked.scope == scope
    assert (
        checked.canonical_contract
        == row.execution_contract
        == prepared.canonical_contract
    )
    assert checked.canonical_ir == row.ir == prepared.canonical_ir
    assert tuple(ObjectReader(checked.canonical_contract).object()) == PARAMETER_MEMBERS
    assert ledger.checkouts == checkouts
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    ["v1", "output", "boolean", "order", "missing", "position", "digest", "types"],
)
async def test_saved_parameter_reader_rejects_inconsistent_whole_binding(
    evaluator: Evaluator, defect: str
) -> None:
    ir, owners = source(ordered=True)
    original = await prepare_execution_contract(
        ir,
        SCOPE,
        owners,
        evaluator,
        binding=publication_binding(),
        parameter_binding=parameter_binding(),
        limits=LIMITS,
    )
    assert isinstance(original, PreparedExecutionContract)
    contract = ObjectReader(original.canonical_contract).object()
    profile = cast(JsonObject, contract["parameter_evaluation"])
    if defect == "v1":
        profile["profile"] = "kora.graph-parameter-evaluation.v1"
    elif defect == "output":
        contract["parameter_evaluation"] = cast(
            JsonObject, binding(SchemaDomain.OUTPUT)
        )
    elif defect == "boolean":
        cast(JsonObject, profile["limits"])["max_value_depth"] = True
    elif defect == "order":
        contract["parameter_evaluation"] = dict(reversed(tuple(profile.items())))
    elif defect == "missing":
        del profile["limits"]
    elif defect == "position":
        contract["parameter_evaluation"] = contract.pop("parameter_evaluation")
    elif defect == "types":
        cast(list[JsonObject], contract["type_contracts"])[0]["schema"] = {
            "type": "string"
        }
    prepared, pin = prepared_case(ObjectReader(ir).object(), contract, SCOPE)
    acquired = transport(prepared, SCOPE)
    if defect == "digest":
        pin = replace(pin, execution_contract_digest="0" * 64)

    with pytest.raises(GraphValueRejected):
        check_detached_execution_contract(
            acquired,
            pin,
            SCOPE,
            ObjectReader(original.resources.text).object(),
            owners,
            limits=LIMITS.reader,
        )

    assert original.canonical_contract != ""


@pytest.mark.asyncio
async def test_legacy_parameter_history_cannot_gain_a_current_holder(
    evaluator: Evaluator,
) -> None:
    ir, owners = source(ordered=True)
    original = await prepare_execution_contract(
        ir,
        SCOPE,
        owners,
        evaluator,
        binding=publication_binding(),
        parameter_binding=parameter_binding(),
        limits=LIMITS,
    )
    assert isinstance(original, PreparedExecutionContract)
    contract = ObjectReader(original.canonical_contract).object()
    del contract["parameter_evaluation"]
    history, pin = prepared_case(ObjectReader(ir).object(), contract, SCOPE)
    physical = ObjectReader(original.resources.text).object()

    legacy = check_detached_execution_contract(
        transport(history, SCOPE), pin, SCOPE, physical, owners, limits=LIMITS.reader
    )
    current = check_detached_execution_contract(
        detached(original),
        GraphVersionPin(
            SCOPE.graph_id, SCOPE.version_id, original.digests.execution_contract_digest
        ),
        SCOPE,
        physical,
        owners,
        limits=LIMITS.reader,
    )

    assert legacy.parameter_evaluation is None
    assert current.parameter_evaluation is not None
    assert tuple(ObjectReader(legacy.canonical_contract).object()) == MEMBERS
    assert legacy.canonical_contract == history.values.execution_contract
    assert legacy.pin.execution_contract_digest != current.pin.execution_contract_digest
