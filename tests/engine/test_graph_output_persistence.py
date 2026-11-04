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
from kora.engine.graph_outputs import read_graph_output_contract
from kora.ir.json import JsonObject, JsonValue
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_output_cases import (
    LIMIT,
    built,
    child,
    output_arguments,
    output_document,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "human_input", "wait_event", "command"])
async def test_graph_output_contract_round_trip(database: Database, kind: str) -> None:
    raw = output_document(kind)
    node = child(raw, "graph", "nodes", "producer")
    schema: JsonObject = {
        "description": "private\0schema",
        "examples": [1, 1.0, -0.0, 10**1000, None],
        "$defs": {"a/b": {"type": "object"}},
        "$ref": "#/$defs/a~1b",
    }
    if kind != "human_input":
        name = "event" if kind == "wait_event" else "value"
        child(node, "outputs", name)["schema"] = schema
    arguments = output_arguments(raw)
    ledger = ConnectionLedger(database)
    expected = built(arguments)
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
    actual = read_graph_output_contract(
        cast(str, row.ir),
        arguments[1],
        arguments[2],
        decoded.execution_contract,
        row.execution_contract_digest,
        max_encoded_bytes=LIMIT,
    )
    altered = ObjectReader(cast(str, row.ir)).object()
    child(altered, "graph", "nodes", "producer", "source")["line"] = 8
    with pytest.raises(GraphValueRejected):
        read_graph_output_contract(
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
    inline = cast(JsonObject, decoded.execution_contract["inline_schema"])
    assert child(inline, "source")["location"] == node["source"]
    if kind == "human_input":
        assert inline["origin"] == "approval_form_default"
    else:
        restored = cast(JsonObject, inline["schema"])
        assert list(restored) == list(schema)
        values = cast(list[JsonValue], restored["examples"])
        assert type(values[0]) is int
        assert type(values[1]) is float
        assert math.copysign(1, cast(float, values[2])) == -1
        assert values[3:] == [10**1000, None]
        assert restored["description"] == "private\0schema"
        assert inline["references"] == [
            {"path": ["$ref"], "target_type": None, "target_path": ["$defs", "a/b"]}
        ]
    assert ledger.opened > 0
    assert ledger.opened == ledger.closed
    assert (
        ledger.outstanding
        == ledger.closed_while_checked_out
        == database.units_in_flight()
        == 0
    )
