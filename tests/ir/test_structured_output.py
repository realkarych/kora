import json
import math
import sys
import tracemalloc
from collections import UserDict
from collections.abc import Callable
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry

from kora.ir import (
    StructuredOutput,
    StructuredOutputValidationError,
    canonical_structured_output,
    parse_structured_output,
    parse_structured_output_json,
    require_structured_output_revision,
    structured_output_digest,
    summarize_structured_output,
)
from kora.ir.json import JsonValue
from kora.ir.structured_json import MAX_JSON_BYTES
from kora.observability.redaction import MASK, SecretRegistry
from tests.ir.structured_output_cases import (
    ARTIFACTS,
    CanonicalCase,
    canonical_cases,
    exact_string,
    forged,
    hostile_inputs,
    nested,
    raw_refusals,
)


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


class HostileString(str):
    def __str__(self) -> str:
        raise AssertionError("untrusted conversion invoked")


class HostileInteger(int):
    def __int__(self) -> int:
        raise AssertionError("untrusted conversion invoked")


class HostileEquality:
    def __eq__(self, other: object) -> bool:
        raise AssertionError("untrusted equality invoked")


def export_value(value: StructuredOutput) -> JsonValue:
    return value.value


@pytest.mark.parametrize("case", canonical_cases(), ids=lambda case: case.name)
def test_structured_output_canonical_numbers(case: CanonicalCase) -> None:
    expected = case.canonical

    value = parse_structured_output_json(case.raw)
    native = parse_structured_output(value.value)

    assert canonical_structured_output(value) == expected
    assert canonical_structured_output(native) == expected
    assert structured_output_digest(value) == case.digest
    assert sha256(expected).hexdigest() == case.digest
    assert canonical_structured_output(StructuredOutput(value.value)) == expected


@pytest.mark.parametrize(
    ("name", "raw"),
    [pytest.param(name, raw, id=name) for name, raw in raw_refusals()],
)
def test_structured_output_rejects_invalid_payload(name: str, raw: bytes) -> None:
    expected = (
        "SO001"
        if len(raw) > MAX_JSON_BYTES
        else "SO004"
        if name == "canonical-over-ceiling-with-bounded-raw"
        else "SO002"
    )

    with pytest.raises(StructuredOutputValidationError) as refused:
        parse_structured_output_json(raw)

    assert refused.value.violation.code == expected
    assert refused.value.violation.path == ()
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


@pytest.mark.parametrize(
    "value",
    [
        HostileString("text"),
        HostileInteger(1),
        HostileEquality(),
        UserDict({"key": 1}),
        Decimal("1"),
        (1, 2),
        {1: "value"},
        {HostileString("key"): 1},
        float("nan"),
        float("inf"),
        "\ud800",
        {"\udfff": 0},
        b"null",
    ],
)
def test_structured_output_native_domain_has_no_coercion(value: object) -> None:
    candidate = value

    with pytest.raises(StructuredOutputValidationError) as refused:
        parse_structured_output(candidate)

    assert refused.value.violation.code == "SO003"


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("bits", [13_999, 14_000, 14_001])
def test_structured_output_integer_magnitude(sign: int, bits: int) -> None:
    number = sign * ((1 << bits) - 1)
    raw = str(number).encode()

    if bits > 14_000:
        with pytest.raises(StructuredOutputValidationError, match="JSON domain"):
            parse_structured_output(number)
        with pytest.raises(StructuredOutputValidationError, match="encoding"):
            parse_structured_output_json(raw)
        return
    value = parse_structured_output(number)
    decoded = parse_structured_output_json(raw)

    assert type(value.value) is int
    assert value.value == number
    assert canonical_structured_output(value) == raw
    assert decoded == value


@pytest.mark.parametrize("name", list(hostile_inputs()))
def test_structured_output_behavior_bounds(name: str) -> None:
    candidate = hostile_inputs()[name]()
    input_bytes = sys.getsizeof(candidate)
    tracemalloc.start()

    try:
        with pytest.raises(StructuredOutputValidationError) as refused:
            parse_structured_output(candidate)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert refused.value.violation.code in {"SO003", "SO004"}
    if name != "shared_tree":
        assert peak < input_bytes


@pytest.mark.parametrize("size", [1_048_575, 1_048_576, 1_048_577])
def test_structured_output_independent_byte_limits(size: int) -> None:
    text = exact_string(size)
    raw = ('"' + text + '"').encode()

    if size > MAX_JSON_BYTES:
        with pytest.raises(StructuredOutputValidationError, match="canonical"):
            parse_structured_output(text)
        with pytest.raises(StructuredOutputValidationError, match="raw JSON"):
            parse_structured_output_json(raw)
        return
    value = parse_structured_output_json(raw)

    assert len(canonical_structured_output(value)) == size
    assert value.value == text


def test_structured_output_depth_and_aliases() -> None:
    shared = {"value": [1, -0.0]}
    aliases = [shared, shared]
    cycle: list[object] = []
    cycle.append(cycle)
    accepted_depth = nested(16)
    rejected_depth = nested(17)

    value = parse_structured_output(aliases)
    depth = parse_structured_output(accepted_depth)
    with pytest.raises(StructuredOutputValidationError, match="JSON domain"):
        parse_structured_output(cycle)
    with pytest.raises(StructuredOutputValidationError, match="JSON domain"):
        parse_structured_output(rejected_depth)

    assert (
        canonical_structured_output(value) == b'[{"value":[1,-0.0]},{"value":[1,-0.0]}]'
    )
    assert canonical_structured_output(depth) == b"[" * 16 + b"0" + b"]" * 16


def test_structured_output_detached_ownership() -> None:
    inner: list[JsonValue] = [1, -0.0, None]
    source: dict[str, JsonValue] = {"key": inner}
    value = parse_structured_output(source)
    digest = structured_output_digest(value)

    source["new"] = "changed"
    inner.append(2)
    exported = cast("dict[str, JsonValue]", value.value)
    cast("list[JsonValue]", exported["key"]).append("changed")
    summary = cast("dict[str, JsonValue]", summarize_structured_output(value))
    summary.clear()

    assert canonical_structured_output(value) == b'{"key":[1,-0.0,null]}'
    assert structured_output_digest(value) == digest
    assert "key" in cast("dict[str, JsonValue]", value.value)
    assert repr(value) == "StructuredOutput()"


@pytest.mark.parametrize(
    "raw",
    [None, 1, b"1.00", b"[", b'"\\ud800"', b" " * 1_048_577],
    ids=[
        "missing",
        "wrong-type",
        "noncanonical",
        "malformed",
        "surrogate",
        "oversized",
    ],
)
@pytest.mark.parametrize(
    "export",
    [
        canonical_structured_output,
        StructuredOutput.canonical_bytes,
        structured_output_digest,
        summarize_structured_output,
        parse_structured_output,
        export_value,
    ],
)
def test_structured_output_unchecked_state_is_revalidated(
    raw: object, export: Callable[[StructuredOutput], object]
) -> None:
    value = forged(raw)

    with pytest.raises(StructuredOutputValidationError) as refused:
        export(value)

    assert refused.value.violation.path == ()


def test_structured_output_missing_state_is_revalidated() -> None:
    value = object.__new__(StructuredOutput)

    with pytest.raises(StructuredOutputValidationError, match="JSON domain"):
        canonical_structured_output(value)

    assert repr(value) == "StructuredOutput()"


def test_structured_output_schema_matches_model() -> None:
    schema = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.schema.json").read_bytes()),
    )
    descriptor: object = json.loads(
        (ARTIFACTS / "StructuredOutput.revision-1.json").read_bytes()
    )
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    require_structured_output_revision(descriptor)
    values = [
        parse_structured_output_json(case.raw).value for case in canonical_cases()
    ]

    assert all(validator.is_valid(value) for value in values)
    assert {type(value) for value in values} == {
        dict,
        list,
        str,
        int,
        float,
        bool,
        type(None),
    }


@pytest.mark.parametrize(
    "replacement", [True, HostileString("1"), HostileEquality(), [], None]
)
def test_structured_output_descriptor_refuses_reinterpretation(
    replacement: object,
) -> None:
    descriptor = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.revision-1.json").read_bytes()),
    )
    descriptor["builtin_revision"] = replacement

    with pytest.raises(StructuredOutputValidationError) as refused:
        require_structured_output_revision(descriptor)

    assert refused.value.violation.code == "SO005"


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("failure", ["duplicate", "malformed"])
def test_structured_output_safe_diagnostics(
    caplog: pytest.LogCaptureFixture, registered: bool, failure: str
) -> None:
    marker = "private-registered-value" if registered else "private-nonsecret-marker"
    registry = SecretRegistry()
    registration = registry.register(marker) if registered else None
    raw = (
        ('{"' + marker + '":1,"' + marker + '":2}').encode()
        if failure == "duplicate"
        else ('{"' + marker + '":').encode()
    )
    caplog.set_level("DEBUG")
    assert registry.mask(marker) == (MASK if registered else marker)

    try:
        with pytest.raises(StructuredOutputValidationError) as refused:
            parse_structured_output_json(raw)
    finally:
        if registration is not None:
            registration.release()

    assert refused.value.violation.model_dump() == {
        "code": "SO002",
        "message": "StructuredOutput JSON encoding is invalid.",
        "path": (),
        "location": None,
        "offset": None,
        "node": None,
        "hint": None,
    }
    assert marker not in str(refused.value)
    assert marker not in repr(refused.value)
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
    assert caplog.records == []
    assert registry.count() == 0


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        pytest.param(b"!" * (MAX_JSON_BYTES + 1), "SO001", id="oversized-malformed"),
        pytest.param(b"!", "SO002", id="malformed"),
    ],
)
def test_structured_output_raw_size_precedes_decoding(
    payload: bytes, code: str
) -> None:
    raw = payload

    with pytest.raises(StructuredOutputValidationError) as refused:
        parse_structured_output_json(raw)

    assert refused.value.violation.code == code


def test_structured_output_null_and_numeric_identity() -> None:
    null = parse_structured_output(None)
    integer = parse_structured_output(1)
    floating = parse_structured_output(1.0)
    negative_zero = parse_structured_output(-0.0)

    values = (null.value, integer.value, floating.value, negative_zero.value)

    assert values[:3] == (None, 1, 1.0)
    assert [type(value) for value in values] == [type(None), int, float, float]
    assert integer != floating
    assert math.copysign(1, cast("float", values[3])) == -1.0
    assert canonical_structured_output(null) == b"null"
