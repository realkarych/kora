import cProfile
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from sqlalchemy import insert, update

from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_values import (
    DecodedGraphValues,
    LegacyGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.db.types import JsonObject, JsonValue
from kora.ir import AgentConfig, export_ir, render_ir_json
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.ir.support import agent_document, deep_data


@pytest.mark.parametrize("depth", [0, 1024, 4096])
def test_graph_value_codec_preserves_canonical_identity(depth: int) -> None:
    schema: JsonObject = {
        "$defs": {"point": {"type": "integer"}},
        "$ref": "#/$defs/point",
        "saved": "kora://types/shape@1",
        "scalars": [None, True, False, 0, 0.0, -0.0, 1e-300, 1e300],
        "strings": ["\0", "\ud800", "\udfff", "\U0001f600", "\ud83d\ude00"],
        "huge": 10**5000 - 1,
        "deep": cast(JsonValue, deep_data(depth)),
    }
    model = AgentConfig.model_validate(agent_document(schema))
    ir = export_ir(model)
    contract = {
        "parameters": {"interpretation": "kora.graph-parameters.v1", "schema": schema}
    }
    settings = (sys.getrecursionlimit(), sys.get_int_max_str_digits())

    encoded = prepare_graph_values(ir, contract, max_encoded_bytes=100_000)
    decoded = read_graph_values(
        1, encoded.ir, encoded.execution_contract, max_encoded_bytes=100_000
    )

    assert isinstance(decoded, DecodedGraphValues)
    assert encoded.ir == render_ir_json(model)
    assert encoded.ir.isascii()
    assert encoded.execution_contract.isascii()
    assert (
        prepare_graph_values(
            decoded.ir, decoded.execution_contract, max_encoded_bytes=100_000
        )
        == encoded
    )
    assert list(decoded.ir) == list(ir)
    restored = cast(JsonObject, decoded.ir["output_schema"])
    assert restored["huge"] == 10**5000 - 1
    assert restored["strings"] == ["\0", "\ud800", "\udfff", "\U0001f600", "\U0001f600"]
    scalars = cast(list[JsonValue], restored["scalars"])
    assert type(scalars[3]) is int
    assert type(scalars[4]) is float
    assert math.copysign(1, cast(float, scalars[5])) == -1
    assert (sys.getrecursionlimit(), sys.get_int_max_str_digits()) == settings


def test_graph_value_codec_refuses_key_collisions() -> None:
    original = {
        "\U0001f600": "first private value",
        "\ud83d\ude00": "second private value",
    }

    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_values(original, {}, max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert str(failure.value) == "invalid graph value representation"
    assert len(original) == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"a":0,"a":1}',
        '{"\\ud83d\\ude00":0,"\\ud83d\\ude00":1}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e999}',
        '{"a":01}',
        '{"a":-0}',
        '{"a":1.00}',
        '{"a":1E0}',
        '{"a":0,}',
        '{"a":[0,]}',
        '{"a": [0]}',
        "{} ",
        "{}{}",
        '{"a":',
        '{"a":"\\q"}',
        '{"a":"\0"}',
        '{"a":"é"}',
        '{"a":"\\u0061"}',
        '{"a":"\\/"}',
        "[]",
        "null",
        '"{}"',
        "",
    ],
)
def test_graph_value_decoder_refuses_noncanonical_or_malformed_text(text: str) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_values(1, text, "{}", max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert str(failure.value) == "invalid graph value representation"


@pytest.mark.parametrize("value", [object(), (1,), {1: 0}, float("nan"), float("inf")])
def test_graph_value_encoder_refuses_non_json_values(value: object) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_values({"value": value}, {}, max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize("budget", [0, -1, True])
def test_graph_value_budget_requires_a_positive_integer(budget: int) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_values({}, {}, max_encoded_bytes=budget)

    assert failure.value.reason is GraphValueFailure.BUDGET


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"a": '\0\ud800\U0001f600\n\t"\\\x7f'},
        {"a": 10**5000 - 1},
        {"a": -(10**5000)},
        {"a": [0] * 200},
        {"a": deep_data(2048)},
    ],
)
def test_graph_value_budget_refuses_oversize_pair(value: object) -> None:
    encoded = prepare_graph_values(value, value, max_encoded_bytes=100_000)
    size = len(encoded.ir) + len(encoded.execution_contract)

    exact = prepare_graph_values(value, value, max_encoded_bytes=size)
    with pytest.raises(GraphValueRejected) as encode_failure:
        prepare_graph_values(value, value, max_encoded_bytes=size - 1)
    with pytest.raises(GraphValueRejected) as decode_failure:
        read_graph_values(
            1, encoded.ir, encoded.execution_contract, max_encoded_bytes=size - 1
        )

    assert exact == encoded
    assert encode_failure.value.reason is GraphValueFailure.BUDGET
    assert decode_failure.value.reason is GraphValueFailure.BUDGET


@pytest.mark.parametrize("digits", [1, 2, 9, 17, 99, 5000])
@pytest.mark.parametrize("negative", [False, True])
def test_graph_value_integer_budget_at_decimal_boundaries(
    digits: int, negative: bool
) -> None:
    integer = (10**digits - 1) * (-1 if negative else 1)
    text = '{"v":' + ("-" if negative else "") + "9" * digits + "}"

    encoded = prepare_graph_values({"v": integer}, {}, max_encoded_bytes=len(text) + 2)
    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_values({"v": integer}, {}, max_encoded_bytes=len(text))

    assert encoded.ir == text
    assert failure.value.reason is GraphValueFailure.BUDGET


def test_graph_value_cycles_refuse_without_globally_banning_shared_fields() -> None:
    field: JsonObject = {"value": 1}
    original = {"first": field, "second": field}
    cycle: list[object] = []
    cycle.append(cycle)

    encoded = prepare_graph_values(original, original, max_encoded_bytes=1000)
    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_values({"cycle": cycle}, {}, max_encoded_bytes=1000)

    assert encoded.ir == '{"first":{"value":1},"second":{"value":1}}'
    assert failure.value.reason is GraphValueFailure.INVALID


@dataclass(frozen=True)
class RefusedWork:
    action: Callable[[], object]
    rendered_strings: int


@pytest.mark.parametrize("case", ["string", "integer", "container", "decode"])
def test_graph_value_budget_refuses_before_expanding_oversized_work(case: str) -> None:
    value: object = "\0" * 100_000 if case == "string" else 10**100_000
    actions = {
        "string": RefusedWork(
            lambda: prepare_graph_values({"v": value}, {}, max_encoded_bytes=8), 1
        ),
        "integer": RefusedWork(
            lambda: prepare_graph_values({"v": value}, {}, max_encoded_bytes=8), 1
        ),
        "container": RefusedWork(
            lambda: prepare_graph_values(
                {"v": [object()] * 100_000}, {}, max_encoded_bytes=6
            ),
            1,
        ),
        "decode": RefusedWork(
            lambda: read_graph_values(
                1, '{"v":' + "0," * 100_000 + "0}", "{}", max_encoded_bytes=8
            ),
            0,
        ),
    }
    chosen = actions[case]
    profile = cProfile.Profile()

    profile.enable()
    try:
        with pytest.raises(GraphValueRejected) as failure:
            chosen.action()
    finally:
        profile.disable()

    assert failure.value.reason is GraphValueFailure.BUDGET
    calls = {
        entry.code: entry.callcount
        for entry in profile.getstats()
        if isinstance(entry.code, str)
    }
    assert (
        sum(count for name, count in calls.items() if "encode_basestring_ascii" in name)
        == chosen.rendered_strings
    )
    assert not any("to_json" in name for name in calls)
    if case == "decode":
        assert not any(
            "raw_decode" == getattr(entry.code, "co_name", None)
            for entry in profile.getstats()
        )


@pytest.mark.parametrize("version", [False, True, 0, 2, "1"])
def test_graph_value_unknown_versions_do_not_fall_back(version: object) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_values(version, {}, {}, max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.VERSION


@pytest.mark.parametrize(("ir", "contract"), [({}, "{}"), ("{}", {}), (None, "{}")])
def test_graph_value_mixed_encoded_rows_refuse(
    ir: JsonValue, contract: JsonValue
) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_values(1, ir, contract, max_encoded_bytes=1000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.asyncio
async def test_graph_version_encoding_round_trip(database: Database) -> None:
    ledger = ConnectionLedger(database)
    source: JsonObject = {
        "nul\0": [10**5000, -0.0, "\ud800", "\U0001f600"],
        "deep": cast(JsonValue, deep_data(1500)),
    }
    encoded = prepare_graph_values(
        source, {"parameters": source}, max_encoded_bytes=100_000
    )
    assert ledger.checkouts == 0
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
        max_encoded_bytes=100_000,
    )
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_values(
            row.value_encoding_version,
            row.ir,
            row.execution_contract,
            max_encoded_bytes=1,
        )
    await database.close()

    assert foreign is None
    assert row.ir == encoded.ir
    assert row.execution_contract == encoded.execution_contract
    assert isinstance(decoded, DecodedGraphValues)
    assert (
        prepare_graph_values(
            decoded.ir, decoded.execution_contract, max_encoded_bytes=100_000
        )
        == encoded
    )
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert ledger.opened == ledger.closed


@pytest.mark.asyncio
async def test_graph_version_encoding_preserves_history(database: Database) -> None:
    historical: JsonObject = {"annotation": "untouched", "parameters": {}}
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        identifier = await seeder.graph_version(graph_id, ir=historical)
    async with database.read_transaction() as connection:
        before = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert before is not None

    legacy = read_graph_values(
        before.value_encoding_version,
        before.ir,
        before.execution_contract,
        max_encoded_bytes=1,
    )
    async with database.read_transaction() as connection:
        after = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    async with database.transaction() as connection:
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == identifier)
            .values(value_encoding_version=1)
        )
    async with database.read_transaction() as connection:
        mixed = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert mixed is not None
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_values(
            mixed.value_encoding_version,
            mixed.ir,
            mixed.execution_contract,
            max_encoded_bytes=1000,
        )

    assert legacy == LegacyGraphValues(historical, {})
    assert before == after
    assert failure.value.reason is GraphValueFailure.INVALID


def test_graph_value_outer_string_storage_bound() -> None:
    encoded = prepare_graph_values(
        {"x": '\0\ud800"\\' * 100}, {}, max_encoded_bytes=10_000
    )

    outer = json.dumps(encoded.ir, ensure_ascii=True) + json.dumps(
        encoded.execution_contract, ensure_ascii=True
    )

    assert len(outer) <= 2 * (len(encoded.ir) + len(encoded.execution_contract)) + 4
