import asyncio
import json
import traceback
from pathlib import Path

import pytest

from kora.conformance.inputs import (
    ConformanceInputError,
    ConformanceInputs,
    InputReason,
)
from kora.ir.json import MAX_CANONICAL_BYTES, JsonObject

PRIVATE_MARKER: str = "private-configuration-marker"


def test_conformance_input_snapshots_are_bounded_and_defensive() -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {"values": {"type": "array", "default": [1]}},
    }
    original: object = json.loads(json.dumps(schema))
    inputs = ConformanceInputs(schema, b'{"values":[1]}', b'{"values":[2]}')
    properties = schema["properties"]
    assert isinstance(properties, dict)

    properties.clear()
    for view in (inputs.schema, inputs.configuration, inputs.fixture_data):
        for value in view.values():
            if isinstance(value, dict | list):
                value.clear()
        view.clear()

    assert inputs.schema == original
    assert inputs.configuration == {"values": [1]}
    assert inputs.fixture_data == {"values": [2]}
    assert repr(inputs) == "ConformanceInputs()"


@pytest.mark.parametrize("field", ["configuration", "fixture_data"])
def test_conformance_data_bounds(field: str) -> None:
    exact = b'{"v":"' + b"a" * (MAX_CANONICAL_BYTES - 8) + b'"}'
    arguments = {field: exact}

    inputs = ConformanceInputs({"type": "object"}, **arguments)

    assert len(exact) == MAX_CANONICAL_BYTES
    assert len(json.dumps(getattr(inputs, field), separators=(",", ":"))) == len(exact)
    with pytest.raises(ConformanceInputError, match=f"^{field}_invalid$"):
        ConformanceInputs({"type": "object"}, **{field: exact + b" "})
    with pytest.raises(ConformanceInputError, match=f"^{field}_invalid$"):
        ConformanceInputs({"type": "object"}, **{field: b'"' + b"a" * 10 + b'"'})


def test_conformance_data_depth_and_canonical_expansion() -> None:
    bounded: JsonObject = {}
    for _ in range(15):
        bounded = {"v": bounded}
    at_limit = json.dumps(bounded).encode()
    past_limit = b'{"v":' + at_limit + b"}"
    expanded = b'{"v":[' + b"1e20," * 179_999 + b"1e20]}"

    inputs = ConformanceInputs({"type": "object"}, at_limit)

    assert inputs.configuration == bounded
    assert len(expanded) < MAX_CANONICAL_BYTES
    with pytest.raises(ConformanceInputError, match="^configuration_invalid$"):
        ConformanceInputs({"type": "object"}, past_limit)
    with pytest.raises(ConformanceInputError, match="^configuration_invalid$"):
        ConformanceInputs({"type": "object"}, expanded)


def test_conformance_schema_bounds() -> None:
    schema: JsonObject = {"type": "object", "title": ""}
    overhead = len(json.dumps(schema, separators=(",", ":")))
    schema["title"] = "a" * (MAX_CANONICAL_BYTES - overhead)
    invalid = {**schema, "title": "a" * (MAX_CANONICAL_BYTES - overhead + 1)}

    inputs = ConformanceInputs(schema)

    assert inputs.schema == schema
    with pytest.raises(ConformanceInputError, match="^schema_invalid$"):
        ConformanceInputs(invalid)


@pytest.mark.parametrize(
    "dialect", [None, "https://json-schema.org/draft/2020-12/schema"]
)
def test_conformance_schema_uses_declared_or_default_dialect(
    dialect: str | None,
) -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {"values": {"prefixItems": [{"type": "integer"}]}},
    }
    if dialect is not None:
        schema["$schema"] = dialect

    accepted = ConformanceInputs(schema, b'{"values":[1]}')

    assert accepted.configuration == {"values": [1]}
    with pytest.raises(ConformanceInputError, match="^configuration_invalid$"):
        ConformanceInputs(schema, b'{"values":["wrong"]}')
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    assert ConformanceInputs(schema, b'{"values":["wrong"]}').configuration == {
        "values": ["wrong"]
    }


@pytest.mark.parametrize(
    "dialect",
    [
        "http://json-schema.org/draft-03/schema#",
        "http://json-schema.org/draft-04/schema#",
        "http://json-schema.org/draft-06/schema#",
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2020-12/schema",
    ],
)
def test_conformance_recognized_dialects(dialect: str) -> None:
    schema = {"type": "object", "$schema": dialect}

    inputs = ConformanceInputs(schema)

    assert inputs.configuration == {}
    assert inputs.fixture_data == {}


def test_conformance_format_annotations_and_required_configuration() -> None:
    schema = {
        "type": "object",
        "required": ["address"],
        "properties": {"address": {"type": "string", "format": "email"}},
    }

    inputs = ConformanceInputs(schema, b'{"address":"not-an-email"}')

    assert inputs.configuration == {"address": "not-an-email"}
    with pytest.raises(ConformanceInputError, match="^configuration_invalid$"):
        ConformanceInputs(schema)


@pytest.mark.parametrize("reference", ["#/$defs/item", "urn:kora:input-item"])
def test_conformance_embedded_references(reference: str) -> None:
    schema = {
        "type": "object",
        "$defs": {"item": {"$id": "urn:kora:input-item", "type": "integer"}},
        "properties": {"value": {"$ref": reference}},
    }

    inputs = ConformanceInputs(schema, b'{"value":1}')

    assert inputs.configuration == {"value": 1}
    with pytest.raises(ConformanceInputError, match="^configuration_invalid$"):
        ConformanceInputs(schema, b'{"value":"wrong"}')


@pytest.mark.asyncio
async def test_conformance_schema_references_remain_offline(tmp_path: Path) -> None:
    requests: list[bytes] = []

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ntrue")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    resource = tmp_path / "schema.json"
    resource.write_text("true")
    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    references = (f"http://127.0.0.1:{port}/schema", resource.as_uri())
    errors: list[InputReason] = []

    async with server, asyncio.timeout(5):
        for reference in references:
            schema = {"type": "object", "$ref": reference}
            with pytest.raises(ConformanceInputError) as caught:
                await asyncio.to_thread(ConformanceInputs, schema)
            errors.append(caught.value.reason)

    assert errors == [InputReason.REFERENCE_UNRESOLVED] * 2
    assert requests == []


def test_conformance_unvisited_and_literal_references() -> None:
    missing = "https://invalid.example/private-configuration-marker"
    schema = {
        "type": "object",
        "$defs": {"unused": {"$ref": missing}},
        "properties": {"optional": {"$ref": missing}},
    }
    configuration = json.dumps({"literal": {"$ref": missing}}).encode()

    inputs = ConformanceInputs(schema, configuration)

    assert inputs.configuration == {"literal": {"$ref": missing}}


@pytest.mark.parametrize(
    ("schema", "configuration", "fixture_data"),
    [
        ({"type": "object", "required": [PRIVATE_MARKER]}, None, None),
        ({"type": "object", "properties": {PRIVATE_MARKER: 1}}, None, None),
        ({"type": "object", "$schema": PRIVATE_MARKER}, None, None),
        ({"type": "object", "$schema": 1}, None, None),
        ({"type": "object", "$schema": f"http://[{PRIVATE_MARKER}]"}, None, None),
        ({"type": "object", "$ref": f"http://[{PRIVATE_MARKER}]"}, None, None),
        (
            {"type": "object", "$id": f"http://[{PRIVATE_MARKER}]", "$ref": "x"},
            None,
            None,
        ),
        ({"type": "object", "$ref": PRIVATE_MARKER}, None, None),
        ({"type": "object", "patternProperties": {"[": {}}}, None, None),
        ({"type": "object"}, PRIVATE_MARKER.encode(), None),
        ({"type": "object"}, None, PRIVATE_MARKER.encode()),
        ({"type": "object"}, b'{"v":NaN}', None),
        ({"type": "object"}, b'{"v":"\\ud800"}', None),
        ({"type": "object"}, b"\xff", None),
        ({"type": "object", "$ref": "#"}, None, None),
    ],
)
def test_conformance_input_failures_are_content_free(
    schema: object, configuration: bytes | None, fixture_data: bytes | None
) -> None:
    arguments = (schema, configuration, fixture_data)

    with pytest.raises(ConformanceInputError) as caught:
        ConformanceInputs(*arguments)

    error = caught.value
    assert str(error) == error.reason.value
    assert PRIVATE_MARKER not in "".join(traceback.format_exception(error))
    assert error.__context__ is None
    assert error.__cause__ is None


def test_conformance_instance_schema_matches_snapshot() -> None:
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"default": 1}}}
    inputs = ConformanceInputs(schema, b'{"x":2}')
    reordered = dict(reversed(tuple(schema.items())))
    changed = {**schema, "required": ["y"]}
    oversized = {"type": "object", "title": "a" * MAX_CANONICAL_BYTES}
    cyclic: dict[str, object] = {"type": "object"}
    cyclic["cycle"] = cyclic

    inputs.require_matching_schema(reordered)

    for invalid in (changed, {**schema, "properties": {"x": {"default": 2}}}):
        with pytest.raises(ConformanceInputError, match="^instance_schema_mismatch$"):
            inputs.require_matching_schema(invalid)
    for invalid in (
        oversized,
        cyclic,
        {"type": "string"},
        {"type": "object", "x": object()},
    ):
        with pytest.raises(ConformanceInputError, match="^instance_schema_invalid$"):
            inputs.require_matching_schema(invalid)
    assert inputs.schema == schema


def test_conformance_schema_evaluation_unavailable() -> None:
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number", "multipleOf": 0.5}},
    }
    oversized_float_conversion = json.dumps({"amount": 10**400}).encode()

    inputs = ConformanceInputs(schema, b'{"amount":4}')
    with pytest.raises(ConformanceInputError) as caught:
        ConformanceInputs(schema, oversized_float_conversion)

    assert inputs.configuration == {"amount": 4}
    assert caught.value.reason is InputReason.SCHEMA_EVALUATION_UNAVAILABLE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
