import math
from collections.abc import Iterator
from typing import Never

import pytest

from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    parse_structured_output_json,
)
from kora.ir.structured_schema_input import SchemaArena
from kora.ir.structured_schema_work import PreparationMeter, PreparationRefused


class AuthoredString(str):
    def __len__(self) -> int:
        raise AssertionError("authored text length invoked")

    def __str__(self) -> str:
        raise AssertionError("authored text conversion invoked")

    def __repr__(self) -> str:
        raise AssertionError("private-equality-text-canary")


class AuthoredInteger(int):
    def bit_length(self) -> int:
        raise AssertionError("authored integer inspection invoked")


class AuthoredFloat(float):
    def __repr__(self) -> str:
        raise AssertionError("authored float spelling invoked")


class AuthoredArray(list[object]):
    def __len__(self) -> int:
        raise AssertionError("authored array length invoked")

    def __iter__(self) -> Iterator[object]:
        raise AssertionError("authored array iteration invoked")


class AuthoredObject(dict[str, object]):
    def __len__(self) -> int:
        raise AssertionError("authored object length invoked")

    def items(self) -> Never:
        raise AssertionError("authored object members invoked")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(AuthoredString("text"), id="text-subclass"),
        pytest.param({AuthoredString("key"): None}, id="key-subclass"),
        pytest.param(AuthoredInteger(1), id="integer-subclass"),
        pytest.param(AuthoredFloat(0.3), id="float-subclass"),
        pytest.param(AuthoredArray([None]), id="array-subclass"),
        pytest.param(AuthoredObject({"key": None}), id="object-subclass"),
        pytest.param(math.nan, id="nonfinite"),
        pytest.param(math.inf, id="positive-infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param(1 << 14_000, id="integer-bits"),
        pytest.param("\ud800", id="surrogate-text"),
        pytest.param({"\udfff": None}, id="surrogate-key"),
    ],
)
def test_actual_model_and_arena_admission_refuse_hostile_domain_without_callbacks(
    value: object,
) -> None:
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(value)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(value)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"
    assert "private-equality-text-canary" not in str(model_rejected.value)
    assert "private-equality-text-canary" not in str(arena_rejected.value)


def test_actual_model_and_arena_admission_refuse_cycles() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(cycle)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(cycle)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize(
    "raw",
    [b"NaN", rb'{"a":0,"\u0061":1}', rb'"\ud800"', rb'{"\udfff":null}'],
)
def test_actual_raw_admission_refuses_duplicate_names_and_surrogate_text(
    raw: bytes,
) -> None:
    candidate = raw

    with pytest.raises(StructuredOutputValidationError) as rejected:
        parse_structured_output_json(candidate)

    assert rejected.value.violation.code == "SO002"
