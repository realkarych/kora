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
from kora.engine.graph_output_inventory import read_graph_output_inventory
from kora.ir.json import JsonValue
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_inventory_cases import (
    complete_inventory,
    entries,
    inventory_arguments,
    mixed_document,
)
from tests.engine.graph_output_cases import LIMIT, child


@pytest.mark.asyncio
async def test_complete_output_inventory_round_trip(database: Database) -> None:
    raw = mixed_document()
    source = child(raw, "graph", "nodes", "producer", "source")
    source["x-source"] = {"z": "\0", "a": -0.0, "values": [1, 1.0, 10**1000]}
    arguments = inventory_arguments(raw)
    text = complete_inventory(arguments)
    contract = {"outputs": entries(text)}
    ledger = ConnectionLedger(database)
    encoded = prepare_graph_values(raw, contract, max_encoded_bytes=LIMIT)
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
                execution_contract_digest=DIGEST,
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
    actual = read_graph_output_inventory(
        cast(str, row.ir),
        arguments[1],
        decoded.execution_contract["outputs"],
        max_encoded_bytes=LIMIT,
    )
    changed = ObjectReader(cast(str, row.ir)).object()
    child(changed, "graph", "nodes", "producer", "outputs", "value")["schema"] = {
        "type": "string"
    }
    with pytest.raises(GraphValueRejected):
        read_graph_output_inventory(
            render_object(changed, TextBudget(LIMIT)),
            arguments[1],
            decoded.execution_contract["outputs"],
            max_encoded_bytes=LIMIT,
        )
    await database.close()

    assert actual == text
    assert row.execution_contract == encoded.execution_contract
    assert decoded.execution_contract == contract
    assert foreign is None
    retained = child(decoded.ir, "graph", "nodes", "producer", "source", "x-source")
    assert list(retained) == ["z", "a", "values"]
    assert retained["z"] == "\0"
    assert math.copysign(1, cast(float, retained["a"])) == -1
    values = cast(list[JsonValue], retained["values"])
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
