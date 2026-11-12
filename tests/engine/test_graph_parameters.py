import sys
from typing import cast

import pytest
from pydantic import MISSING
from sqlalchemy import insert

from kora.db.connection import Database
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    render_object,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.db.types import JsonObject, JsonValue
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.ir import GraphDocument, IrJsonObjectField, export_ir, render_ir_json
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.ir.document_cases import document
from tests.ir.support import deep_data
from tests.support.call_observation import ObservedCalls, observing_calls

INTERPRETATION = "kora.graph-parameters.v1"
CLOSED_EMPTY: JsonObject = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
AUTHORED: JsonObject = {
    "$defs": {"value": {"type": "integer"}},
    "$ref": "#/$defs/value",
    "saved": "kora://types/value@1",
    "description": "\0\ud800\U0001f600",
    "examples": [1, 1.0, -0.0],
}


def parameter_document(schema: object = MISSING) -> GraphDocument:
    raw = document()
    if schema is not MISSING:
        cast(dict[str, object], raw["graph"])["params"] = schema
    return GraphDocument.model_validate(raw)


@pytest.mark.parametrize("schema", [MISSING, {}, CLOSED_EMPTY, AUTHORED])
def test_graph_parameter_binding_preserves_authored_and_omitted_schema(
    schema: object,
) -> None:
    pinned = parameter_document(schema)
    before = render_ir_json(pinned)
    effective = CLOSED_EMPTY if schema is MISSING else schema

    text = build_graph_parameter_binding(pinned, max_encoded_bytes=10_000)
    saved = ObjectReader(text).object()
    verified = read_graph_parameter_binding(pinned, saved, max_encoded_bytes=10_000)
    reordered = {"schema": saved["schema"], "interpretation": saved["interpretation"]}
    reversed_text = read_graph_parameter_binding(
        pinned, reordered, max_encoded_bytes=10_000
    )

    assert saved == {"interpretation": INTERPRETATION, "schema": effective}
    assert text == verified
    assert reversed_text.startswith('{"schema":')
    assert ObjectReader(reversed_text).object() == saved
    assert render_ir_json(pinned) == before
    assert (pinned.graph.params is MISSING) is (schema is MISSING)
    if schema is not MISSING:
        assert type(pinned.graph.params) is IrJsonObjectField


@pytest.mark.parametrize(
    ("saved", "reason"),
    [
        (None, GraphValueFailure.INVALID),
        ({}, GraphValueFailure.INVALID),
        ({"schema": {}}, GraphValueFailure.INVALID),
        ({"interpretation": INTERPRETATION, "other": {}}, GraphValueFailure.INVALID),
        (
            {"interpretation": INTERPRETATION, "schema": {}, "extra": 1},
            GraphValueFailure.INVALID,
        ),
        (
            {"interpretation": "private unsupported", "schema": {}},
            GraphValueFailure.VERSION,
        ),
        ({"interpretation": [], "schema": {}}, GraphValueFailure.VERSION),
        ({"interpretation": INTERPRETATION, "schema": []}, GraphValueFailure.INVALID),
        ({"interpretation": INTERPRETATION, "schema": {}}, GraphValueFailure.INVALID),
        (
            {"interpretation": INTERPRETATION, "schema": {"x": object()}},
            GraphValueFailure.INVALID,
        ),
        (
            {"interpretation": INTERPRETATION, "schema": {"x": float("nan")}},
            GraphValueFailure.INVALID,
        ),
    ],
)
def test_graph_parameter_binding_refuses_inconsistent_saved_values(
    saved: object, reason: GraphValueFailure
) -> None:
    pinned = parameter_document()

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_parameter_binding(pinned, saved, max_encoded_bytes=10_000)

    assert failure.value.reason is reason
    assert str(failure.value) == reason.value
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(("original", "changed"), [(1, 1.0), (0.0, -0.0), (0, False)])
def test_graph_parameter_binding_preserves_numeric_identity(
    original: JsonValue, changed: JsonValue
) -> None:
    pinned = parameter_document({"value": original})
    saved = {"interpretation": INTERPRETATION, "schema": {"value": changed}}

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_parameter_binding(pinned, saved, max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.INVALID


def test_graph_parameter_binding_uses_canonical_unicode_identity() -> None:
    pinned = parameter_document({"\ud83d\ude00": "\ud83d\ude00"})
    saved = {"interpretation": INTERPRETATION, "schema": {"\U0001f600": "\U0001f600"}}
    collision = {"\ud83d\ude00": 0, "\U0001f600": 1}
    colliding_ir = parameter_document(collision)

    text = read_graph_parameter_binding(pinned, saved, max_encoded_bytes=1000)
    with pytest.raises(GraphValueRejected) as build_failure:
        build_graph_parameter_binding(colliding_ir, max_encoded_bytes=1000)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_parameter_binding(
            pinned,
            {"interpretation": INTERPRETATION, "schema": collision},
            max_encoded_bytes=1000,
        )

    assert text == build_graph_parameter_binding(pinned, max_encoded_bytes=1000)
    assert (
        build_failure.value.reason
        is read_failure.value.reason
        is GraphValueFailure.INVALID
    )


@pytest.mark.parametrize(
    "schema",
    [{}, {"x": '\0\ud800\U0001f600\n"\\'}, {"x": 10**5000 - 1}, {"x": deep_data(2048)}],
)
def test_graph_parameter_binding_is_bounded(schema: object) -> None:
    pinned = parameter_document(schema)
    text = build_graph_parameter_binding(pinned, max_encoded_bytes=100_000)
    saved = ObjectReader(text).object()
    size = len(text)
    settings = (sys.getrecursionlimit(), sys.get_int_max_str_digits())

    exact = build_graph_parameter_binding(pinned, max_encoded_bytes=size)
    verified = read_graph_parameter_binding(pinned, saved, max_encoded_bytes=size)
    with pytest.raises(GraphValueRejected) as build_failure:
        build_graph_parameter_binding(pinned, max_encoded_bytes=size - 1)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_parameter_binding(pinned, saved, max_encoded_bytes=size - 1)

    assert exact == verified == text
    assert (
        build_failure.value.reason
        is read_failure.value.reason
        is GraphValueFailure.BUDGET
    )
    assert (sys.getrecursionlimit(), sys.get_int_max_str_digits()) == settings


@pytest.mark.parametrize("budget", [0, -1, True])
def test_graph_parameter_binding_requires_positive_integer_budget(budget: int) -> None:
    pinned = parameter_document({})
    saved: JsonObject = {"interpretation": INTERPRETATION, "schema": {}}

    with pytest.raises(GraphValueRejected) as build_failure:
        build_graph_parameter_binding(pinned, max_encoded_bytes=budget)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_parameter_binding(pinned, saved, max_encoded_bytes=budget)

    assert (
        build_failure.value.reason
        is read_failure.value.reason
        is GraphValueFailure.BUDGET
    )


def test_graph_parameter_binding_admits_root_before_copy_or_visitation() -> None:
    pinned = parameter_document({str(index): 0 for index in range(10_000)})
    assert pinned.graph.params is not MISSING
    cast(dict[str, object], pinned.graph.params)["late invalid"] = object()
    calls = ObservedCalls()

    with (
        pytest.raises(GraphValueRejected) as failure,
        observing_calls(calls, build_graph_parameter_binding.__code__),
    ):
        build_graph_parameter_binding(pinned, max_encoded_bytes=64)

    assert failure.value.reason is GraphValueFailure.BUDGET
    assert calls.functions
    assert not any("copy" in name or "items" in name for name in calls.builtins)
    assert render_object.__code__ not in calls.functions


@pytest.mark.parametrize("cycle", [False, True])
def test_graph_parameter_binding_refuses_post_parse_aliases(cycle: bool) -> None:
    pinned = parameter_document({"first": []})
    assert pinned.graph.params is not MISSING
    shared = cast(list[JsonValue], pinned.graph.params["first"])
    if cycle:
        shared.append(shared)
    else:
        pinned.graph.params["second"] = shared
    saved = {"interpretation": INTERPRETATION, "schema": dict(pinned.graph.params)}

    with pytest.raises(GraphValueRejected) as build_failure:
        build_graph_parameter_binding(pinned, max_encoded_bytes=1000)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_parameter_binding(pinned, saved, max_encoded_bytes=1000)

    assert (
        build_failure.value.reason
        is read_failure.value.reason
        is GraphValueFailure.INVALID
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [MISSING, {}, AUTHORED])
async def test_graph_parameter_binding_round_trip(
    database: Database, schema: object
) -> None:
    ledger = ConnectionLedger(database)
    pinned = parameter_document(schema)
    text = build_graph_parameter_binding(pinned, max_encoded_bytes=10_000)
    encoded = prepare_graph_values(
        export_ir(pinned),
        {"parameters": ObjectReader(text).object()},
        max_encoded_bytes=20_000,
    )
    assert ledger.checkouts == database.units_in_flight() == 0
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        foreign_id = await seeder.namespace("foreign")
        graph_id = await seeder.graph(namespace_id)
        await connection.execute(
            insert(graph_version).values(
                id=seeder.new_id(),
                graph_id=graph_id,
                number=1,
                ir=encoded.ir,
                execution_contract=encoded.execution_contract,
                value_encoding_version=encoded.value_encoding_version,
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
        row = await graph_version_by_number(connection, namespace_id, graph_id, 1)
        foreign = await graph_version_by_number(connection, foreign_id, graph_id, 1)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert row is not None
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=20_000,
    )
    assert isinstance(decoded, DecodedGraphValues)
    restored = GraphDocument.model_validate(decoded.ir)
    verified = read_graph_parameter_binding(
        restored,
        decoded.execution_contract["parameters"],
        max_encoded_bytes=10_000,
    )
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_parameter_binding(
            parameter_document({"substituted": True}),
            decoded.execution_contract["parameters"],
            max_encoded_bytes=10_000,
        )
    await database.close()

    assert verified == text
    assert render_ir_json(restored) == render_ir_json(pinned)
    assert foreign is None
    assert failure.value.reason is GraphValueFailure.INVALID
    assert ledger.opened > 0
    assert (
        ledger.outstanding
        == ledger.closed_while_checked_out
        == database.units_in_flight()
        == 0
    )
    assert ledger.opened == ledger.closed
