import cProfile
import hashlib
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest

from kora.db.connection import Database
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.engine.graph_type_references import child_path, path_text
from kora.engine.graph_types import (
    build_graph_type_contracts,
    read_graph_type_contracts,
)
from kora.ir import GraphDocument, render_ir_json
from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_output import structured_output_revision_descriptor
from tests.db.closing import ConnectionLedger
from tests.db.seed import stored_graph_version
from tests.ir.document_cases import document
from tests.ir.support import deep_data

BUILTINS = ("AgentSession", "CommandResult", "StructuredOutput", "VcsRef")


def custom(schema: JsonObject, parent: str | None = None) -> JsonObject:
    return {
        "schema": {"$schema": SCHEMA_DIALECT, **schema},
        "parent": parent,
        "behavior": {
            "validation": "example.validation.v1",
            "digest_fields": "example.digest.v1",
            "summary": "example.summary.v1",
        },
    }


def canonical_ir(contracts: JsonObject) -> str:
    raw = document()
    raw["type_contracts"] = contracts
    return render_ir_json(GraphDocument.model_validate(raw))


def section_value(text: str) -> list[JsonValue]:
    value = ObjectReader('{"v":' + text + "}").object()["v"]
    assert isinstance(value, list)
    return value


@pytest.mark.parametrize("mode", ["empty", "builtins", "custom"])
def test_graph_type_contract_inventory(mode: str) -> None:
    contracts: JsonObject = (
        {}
        if mode == "empty"
        else {name: {"builtin_revision": "1"} for name in BUILTINS}
    )
    schema: JsonObject = {
        "description": "\0\ud800\ud83d\ude00",
        "examples": [1, 1.0, -0.0, 10**5000 - 1],
        "$defs": {"value": {"type": "integer"}},
        "$ref": "#/$defs/value",
    }
    if mode == "custom":
        contracts["example/Unused"] = custom(schema, "StructuredOutput")
    ir_text = canonical_ir(contracts)
    leaked = structured_output_revision_descriptor()
    leaked["name"] = "mutated returned metadata"

    text = build_graph_type_contracts(ir_text, max_encoded_bytes=100_000)
    saved = section_value(text)
    verified = read_graph_type_contracts(ir_text, saved, max_encoded_bytes=100_000)

    assert verified == text
    records = [cast(JsonObject, item) for item in saved]
    assert [record["name"] for record in records] == sorted(contracts)
    for record in records:
        assert list(record) == [
            "name",
            "builtin_revision",
            "parent",
            "behavior",
            "descriptor",
            "source",
            "schema",
            "references",
        ]
        name = cast(str, record["name"])
        assert record["source"] == {
            "kind": "type_schema",
            "path": ["type_contracts", name, "schema"],
            "location": None,
        }
        if name in BUILTINS:
            raw_schema = (
                files("kora.ir")
                .joinpath("artifact_schemas", name + ".schema.json")
                .read_bytes()
            )
            published = (
                Path(__file__).parents[2]
                / "knowledge/specs/artifacts"
                / (name + ".revision-1.json")
            )
            descriptor = cast(JsonObject, record["descriptor"])
            assert descriptor == json.loads(published.read_bytes())
            assert descriptor["schema_sha256"] == hashlib.sha256(raw_schema).hexdigest()
            assert record["schema"] == json.loads(raw_schema)
        else:
            restored = cast(JsonObject, record["schema"])
            assert list(restored) == ["$schema", *schema]
            assert restored["description"] == "\0\ud800\U0001f600"
            assert record["parent"] == "StructuredOutput"
            assert record["descriptor"] is None
    assert canonical_ir(contracts) == ir_text


def test_graph_type_contract_references_lossless() -> None:
    leaf: JsonObject = {"$ref": "#/$defs/value"}
    nested: JsonObject = leaf
    for _ in range(80):
        nested = {"properties": {"next": nested}}
    deep_pointer = "/$defs/deep" + "/properties/next" * 80
    schema: JsonObject = {
        "$defs": {
            "value": {"type": "string"},
            "a/b~c": True,
            "0": False,
            "deep": nested,
        },
        "prefixItems": [True],
        "properties": {
            "same": {"$ref": "#/$defs/value"},
            "other": {"$dynamicRef": "urn:kora:artifact:example:Other#/$defs/value"},
            "escaped": {"$recursiveRef": "#/%24defs/a~1b~0c"},
            "named": {"$ref": "#/$defs/0"},
            "indexed": {"$ref": "#/prefixItems/0"},
            "deep": {"$ref": "#" + deep_pointer},
        },
        "dependencies": {"schema": {"$ref": "#"}, "data": ["$ref"]},
        "annotation": {"$ref": "https://private.invalid/not-a-schema"},
    }
    ir_text = canonical_ir(
        {
            "example/Main": custom(schema),
            "example/Other": custom(
                {
                    "$defs": {"value": {"type": "integer"}},
                    "$ref": "urn:kora:artifact:example:Main#",
                }
            ),
        }
    )

    text = build_graph_type_contracts(ir_text, max_encoded_bytes=100_000)
    records = [cast(JsonObject, row) for row in section_value(text)]
    references = [
        cast(JsonObject, row) for row in cast(list[JsonValue], records[0]["references"])
    ]
    paths = {tuple(cast(list[str | int], row["path"])): row for row in references}

    assert len(references) == 8
    assert paths[("properties", "same", "$ref")]["target_type"] == "example/Main"
    assert (
        paths[("properties", "other", "$dynamicRef")]["target_type"] == "example/Other"
    )
    assert paths[("properties", "escaped", "$recursiveRef")]["target_path"] == [
        "$defs",
        "a/b~c",
    ]
    assert paths[("properties", "named", "$ref")]["target_path"] == ["$defs", "0"]
    assert paths[("properties", "indexed", "$ref")]["target_path"] == ["prefixItems", 0]
    assert paths[("properties", "deep", "$ref")]["target_path"] == [
        "$defs",
        "deep",
        *(["properties", "next"] * 80),
    ]
    assert ("$defs", "deep", *(["properties", "next"] * 80), "$ref") in paths
    assert records[1]["references"] == [
        {"path": ["$ref"], "target_type": "example/Main", "target_path": []}
    ]
    assert (
        read_graph_type_contracts(
            ir_text, section_value(text), max_encoded_bytes=100_000
        )
        == text
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "name",
        "builtin_revision",
        "parent",
        "behavior",
        "descriptor",
        "source",
        "schema",
        "references",
        "order",
        "target",
    ],
)
def test_graph_type_contract_rejects_inconsistent_saved_values(change: str) -> None:
    ir_text = canonical_ir(
        {"example/Type": custom({"$defs": {"x": True}, "$ref": "#/$defs/x"})}
    )
    saved = section_value(build_graph_type_contracts(ir_text, max_encoded_bytes=10_000))
    record = cast(JsonObject, saved[0])
    if change == "missing":
        record.pop("schema")
    elif change == "extra":
        record["extra"] = "private changed value"
    elif change == "order":
        saved[0] = dict(reversed(list(record.items())))
    elif change == "target":
        reference = cast(JsonObject, cast(list[JsonValue], record["references"])[0])
        reference["target_path"] = []
    else:
        record[change] = "private changed value"

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_type_contracts(ir_text, saved, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert str(failure.value) == "invalid graph value representation"


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "order", "number"])
def test_graph_type_contract_rejects_changed_inventory(change: str) -> None:
    ir_text = canonical_ir(
        {
            "example/Alpha": custom({"examples": [1]}),
            "example/Beta": custom({}),
        }
    )
    saved = section_value(build_graph_type_contracts(ir_text, max_encoded_bytes=10_000))
    if change == "missing":
        saved.pop()
    elif change == "extra":
        extra = cast(JsonObject, saved[0]).copy()
        extra["name"] = "example/Extra"
        saved.append(extra)
    elif change == "duplicate":
        saved.append(saved[0])
    elif change == "order":
        saved.reverse()
    else:
        cast(JsonObject, cast(JsonObject, saved[0])["schema"])["examples"] = [1.0]

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_type_contracts(ir_text, saved, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize("change", ["missing", "duplicate", "order", "path-kind"])
def test_graph_type_contract_rejects_changed_reference_inventory(change: str) -> None:
    ir_text = canonical_ir(
        {
            "example/Type": custom(
                {
                    "prefixItems": [True],
                    "properties": {
                        "z": {"$ref": "#/prefixItems/0"},
                        "a": {"$ref": "#"},
                    },
                }
            )
        }
    )
    saved = section_value(build_graph_type_contracts(ir_text, max_encoded_bytes=10_000))
    references = cast(list[JsonValue], cast(JsonObject, saved[0])["references"])
    assert cast(JsonObject, references[0])["path"] == ["properties", "a", "$ref"]
    if change == "missing":
        references.pop()
    elif change == "duplicate":
        references.append(references[0])
    elif change == "order":
        references.reverse()
    else:
        cast(JsonObject, references[1])["target_path"] = ["prefixItems", "0"]

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_type_contracts(ir_text, saved, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize("value", [None, {}, (), "[]", [object()]])
def test_graph_type_contract_rejects_non_section_values(value: object) -> None:
    ir_text = canonical_ir({})

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_type_contracts(ir_text, value, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "urn:kora:artifact:example:Missing#"},
        {"$ref": "https://private.invalid/schema"},
        {"$ref": "#anchor", "$anchor": "anchor"},
        {"$defs": {"x": {"$id": "nested"}}, "$ref": "#/$defs/x"},
        {"description": {"$ref": "#"}, "$ref": "#/description"},
        {"prefixItems": [True], "$ref": "#/prefixItems/00"},
    ],
)
def test_graph_type_contract_rejects_invalid_reference_sources(
    schema: JsonObject,
) -> None:
    ir_text = canonical_ir({"example/Type": custom(schema)})

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_type_contracts(ir_text, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize(
    "contracts",
    [
        {"StructuredOutput": {"builtin_revision": "2"}},
        {"example/Type": custom({}, "example/Missing")},
        {"example/Type": custom({}, "example/Type")},
    ],
)
def test_graph_type_contract_rejects_invalid_declarations(
    contracts: JsonObject,
) -> None:
    ir_text = canonical_ir(contracts)

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_type_contracts(ir_text, max_encoded_bytes=10_000)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize(
    "text", ["", "{}", "[]", "{} ", '{"private":"value",}', '{"ir_version":"private"}']
)
def test_graph_type_contract_rejects_invalid_canonical_ir(text: str) -> None:
    with pytest.raises(GraphValueRejected) as failure:
        build_graph_type_contracts(text, max_encoded_bytes=1000)

    assert str(failure.value) == "invalid graph value representation"
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(
    "value",
    [None, '\0\ud800\U0001f600\n"\\', 10**5000 - 1, deep_data(1500)],
    ids=["empty", "escaped", "large-integer", "deep"],
)
def test_graph_type_contract_capacity(value: object) -> None:
    contracts: JsonObject = (
        {}
        if value is None
        else {
            "example/Type": custom({"annotation": cast(JsonValue, value), "$ref": "#"})
        }
    )
    ir_text = canonical_ir(contracts)
    text = build_graph_type_contracts(ir_text, max_encoded_bytes=100_000)
    saved = section_value(text)
    exact_size = len(ir_text) + len(text)
    settings = (sys.getrecursionlimit(), sys.get_int_max_str_digits())

    exact = build_graph_type_contracts(ir_text, max_encoded_bytes=exact_size)
    verified = read_graph_type_contracts(ir_text, saved, max_encoded_bytes=exact_size)
    with pytest.raises(GraphValueRejected) as build_failure:
        build_graph_type_contracts(ir_text, max_encoded_bytes=exact_size - 1)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_type_contracts(ir_text, saved, max_encoded_bytes=exact_size - 1)
    with pytest.raises(GraphValueRejected) as input_failure:
        build_graph_type_contracts(ir_text, max_encoded_bytes=len(ir_text) - 1)

    assert exact == verified == text
    assert (
        build_failure.value.reason
        is read_failure.value.reason
        is input_failure.value.reason
        is GraphValueFailure.BUDGET
    )
    assert (sys.getrecursionlimit(), sys.get_int_max_str_digits()) == settings


def test_graph_type_path_capacity_precedes_segment_materialization() -> None:
    path = child_path(None, "private")
    for _ in range(10_000):
        path = child_path(path, "private")
    budget = TextBudget(4)
    profile = cProfile.Profile()

    profile.enable()
    try:
        with pytest.raises(GraphValueRejected) as failure:
            path_text(path, budget)
    finally:
        profile.disable()

    assert failure.value.reason is GraphValueFailure.BUDGET
    assert not any(
        getattr(entry.code, "co_name", None) == "array_text"
        for entry in profile.getstats()
    )
    path_calls = [
        call.code
        for entry in profile.getstats()
        if entry.code is path_text.__code__
        for call in entry.calls or []
        if isinstance(call.code, str)
    ]
    assert not any("append" in name or "reverse" in name for name in path_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "builtins", "custom"])
async def test_graph_type_contract_round_trip(database: Database, mode: str) -> None:
    contracts: JsonObject = (
        {}
        if mode == "empty"
        else {name: {"builtin_revision": "1"} for name in BUILTINS}
    )
    if mode == "custom":
        contracts["example/Type"] = custom(
            {"description": "\0\ud800", "examples": [1, 1.0, -0.0], "$ref": "#"},
            "StructuredOutput",
        )
    ir_text = canonical_ir(contracts)
    ledger = ConnectionLedger(database)
    text = build_graph_type_contracts(ir_text, max_encoded_bytes=100_000)
    encoded = prepare_graph_values(
        ObjectReader(ir_text).object(),
        {"type_contracts": section_value(text)},
        max_encoded_bytes=200_000,
    )
    assert ledger.checkouts == database.units_in_flight() == 0
    row, foreign = await stored_graph_version(database, encoded)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert row is not None
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=200_000,
    )
    assert isinstance(decoded, DecodedGraphValues)
    verified = read_graph_type_contracts(
        cast(str, row.ir),
        decoded.execution_contract["type_contracts"],
        max_encoded_bytes=100_000,
    )
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_type_contracts(
            canonical_ir({"example/Substitution": custom({})}),
            decoded.execution_contract["type_contracts"],
            max_encoded_bytes=100_000,
        )
    await database.close()

    assert verified == text
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
