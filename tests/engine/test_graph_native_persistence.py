import math
from typing import cast

import pytest
from sqlalchemy import insert

from kora.db.connection import Database
from kora.db.graph_value_json import (
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_native_outputs import (
    build_graph_native_output_contract,
    read_graph_native_output_contract,
)
from kora.ir.json import JsonValue
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_native_cases import native_arguments, native_document
from tests.engine.graph_output_cases import LIMIT, child


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["AgentSession", "CommandResult", "VcsRef"])
async def test_native_output_contract_round_trip(database: Database, name: str) -> None:
    raw = native_document(name)
    node = child(raw, "graph", "nodes", "producer")
    child(node, "source")["x-source"] = {
        "z": "\0",
        "a": -0.0,
        "values": [1, 1.0, 10**1000],
    }
    arguments = native_arguments(name, raw=raw)
    ledger = ConnectionLedger(database)
    expected = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    encoded = prepare_graph_values(
        raw, ObjectReader(expected.text).object(), max_encoded_bytes=LIMIT
    )
    assert ledger.checkouts == database.units_in_flight() == 0
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        foreign_namespace = await seeder.namespace("foreign")
        graph = await seeder.graph(namespace)
        await connection.execute(
            insert(graph_version).values(
                id=seeder.new_id(),
                graph_id=graph,
                number=1,
                ir=encoded.ir,
                execution_contract=encoded.execution_contract,
                value_encoding_version=1,
                ir_digest=DIGEST,
                source_digest=DIGEST,
                execution_contract_digest=expected.digest,
                deployment_digest=DIGEST,
                resolved_digests={},
                resources={},
                deployed_by="owner",
                deployed_at=seeder.clock.now(),
                status="active",
                **seeder.stamps(),
            )
        )

    async with database.read_transaction() as connection:
        row = await graph_version_by_number(connection, namespace, graph, 1)
        foreign = await graph_version_by_number(connection, foreign_namespace, graph, 1)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert row is not None
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    assert isinstance(decoded, DecodedGraphValues)
    actual = read_graph_native_output_contract(
        cast(str, row.ir),
        arguments[1],
        arguments[2],
        decoded.execution_contract,
        row.execution_contract_digest,
        max_encoded_bytes=LIMIT,
    )
    altered = ObjectReader(cast(str, row.ir)).object()
    child(altered, "graph", "nodes", "producer")["kind"] = "timer"
    child(altered, "graph", "nodes", "producer")["config"] = {"duration": "0s"}
    with pytest.raises(GraphValueRejected):
        read_graph_native_output_contract(
            render_object(altered, TextBudget(LIMIT)),
            arguments[1],
            arguments[2],
            decoded.execution_contract,
            row.execution_contract_digest,
            max_encoded_bytes=LIMIT,
        )
    await database.close()

    assert actual == expected
    assert row.execution_contract == expected.text
    assert foreign is None
    assert decoded.execution_contract == ObjectReader(expected.text).object()
    source = child(decoded.ir, "graph", "nodes", "producer", "source", "x-source")
    assert source["z"] == "\0"
    assert math.copysign(1, cast(float, source["a"])) == -1
    values = cast(list[JsonValue], source["values"])
    assert type(values[0]) is int
    assert type(values[1]) is float
    assert values[2] == 10**1000
    assert ledger.opened > 0
    assert ledger.opened == ledger.closed
    assert (
        ledger.outstanding
        == ledger.closed_while_checked_out
        == database.units_in_flight()
        == 0
    )
