import json
import sys
import weakref
from typing import cast

import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_validation import validate_standard_schema
from kora.ir.structured_output import (
    StructuredOutput,
    canonical_structured_output,
    structured_output_digest,
)
from tests.ir.standard_schema_cases import binding, source
from tests.ir.structured_output_cases import ARTIFACTS


@pytest.mark.parametrize(
    "name",
    [
        "decimal-multiple",
        "wide-integer-half-multiple",
        "decimal-canonical-equality",
        "signed-zero-equality",
    ],
)
def test_standard_schema_executes_published_scalar_vectors(name: str) -> None:
    vectors = cast(
        "dict[str, list[dict[str, object]]]",
        json.loads((ARTIFACTS / "StructuredOutput.vectors.json").read_bytes()),
    )
    case = next(
        case for case in vectors["schema_arithmetic_cases"] if case["name"] == name
    )
    candidate = source(json.loads(cast("str", case["schema_text"])))
    value: object = json.loads(cast("str", case["value_text"]))

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if case["accepted"] else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("left", "right", "order"),
    [
        pytest.param(1e23, 100_000_000_000_000_000_000_000, 0, id="frozen-decimal"),
        pytest.param(1e23, 99_999_999_999_999_999_999_999, 1, id="below-decimal"),
        pytest.param(1e23, 100_000_000_000_000_000_000_001, -1, id="above-decimal"),
        pytest.param(0, -0.0, 0, id="integer-negative-float-zero"),
        pytest.param(-0.0, 0.0, 0, id="signed-floating-zero"),
        pytest.param(-0.3, -0.2, -1, id="same-negative-sign"),
        pytest.param(0.3, 0.2, 1, id="same-positive-sign"),
        pytest.param(0, 5e-324, -1, id="zero-subnormal"),
        pytest.param(5e-324, 1e-323, -1, id="subnormal-order"),
        pytest.param(
            sys.float_info.max,
            17_976_931_348_623_157 * 10**292,
            0,
            id="largest-finite-decimal",
        ),
        pytest.param((1 << 14_000) - 1, (1 << 14_000) - 2, 1, id="14000-bit-order"),
        pytest.param(-(1 << 1000), -5e-324, -1, id="negative-wide-and-subnormal"),
    ],
)
def test_standard_schema_retains_numerical_order_and_equality(
    left: int | float, right: int | float, order: int
) -> None:
    equal_source = source({"const": right})
    forward_source = source({"exclusiveMaximum": right})
    reverse_source = source({"exclusiveMaximum": left})
    original = json.dumps([left, right, equal_source, forward_source, reverse_source])

    equal = validate_standard_schema(left, equal_source, SchemaDomain.OUTPUT, binding())
    forward = validate_standard_schema(
        left, forward_source, SchemaDomain.OUTPUT, binding()
    )
    reverse = validate_standard_schema(
        right, reverse_source, SchemaDomain.OUTPUT, binding()
    )

    assert equal.outcome == ("valid" if order == 0 else "invalid")
    assert forward.outcome == ("valid" if order < 0 else "invalid")
    assert reverse.outcome == ("valid" if order > 0 else "invalid")
    assert equal.reason == forward.reason == reverse.reason == "none"
    assert (
        json.dumps([left, right, equal_source, forward_source, reverse_source])
        == original
    )


def test_standard_schema_maximum_float_is_an_integer_multiple() -> None:
    value = sys.float_info.max
    candidate = source({"multipleOf": 1})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "valid"
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param(-1.0, True, id="negative-integral-float"),
        pytest.param(-1.5, False, id="negative-fractional-float"),
        pytest.param(sys.float_info.max, True, id="maximum-finite"),
        pytest.param((1 << 14_000) - 1, True, id="maximum-integer-bits"),
    ],
)
def test_standard_schema_retains_numeric_integer_classification(
    value: int | float, valid: bool
) -> None:
    candidate = source({"type": "integer"})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


def test_standard_schema_numerical_equality_preserves_payload_identity() -> None:
    integer = StructuredOutput(1)
    floating = StructuredOutput(1.0)
    positive_zero = StructuredOutput(0.0)
    negative_zero = StructuredOutput(-0.0)
    one_source = source({"const": floating.value})
    zero_source = source({"const": negative_zero.value})

    same_one = validate_standard_schema(
        integer.value, one_source, SchemaDomain.OUTPUT, binding()
    )
    same_zero = validate_standard_schema(
        positive_zero.value, zero_source, SchemaDomain.OUTPUT, binding()
    )

    assert same_one.outcome == same_zero.outcome == "valid"
    assert same_one.reason == same_zero.reason == "none"
    assert canonical_structured_output(integer) == b"1"
    assert canonical_structured_output(floating) == b"1.0"
    assert canonical_structured_output(positive_zero) == b"0.0"
    assert canonical_structured_output(negative_zero) == b"-0.0"
    assert structured_output_digest(integer) != structured_output_digest(floating)
    assert structured_output_digest(positive_zero) != structured_output_digest(
        negative_zero
    )


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize("position", ["value", "schema"])
def test_standard_schema_native_boundary_refuses_authored_metaclasses(
    domain: SchemaDomain, position: str
) -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("equality")
            raise AssertionError("authored metaclass equality invoked")

    class Authored(metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("representation")
            raise AssertionError("private-numeric-canary")

    authored = Authored()
    retained = weakref.ref(authored)
    value: object = authored if position == "value" else None
    candidate = source(True if position == "value" else {"const": authored})

    result = validate_standard_schema(value, candidate, domain, binding(domain))
    del authored
    del value
    del candidate

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"
    assert calls == []
    assert "private-numeric-canary" not in str(result)
    assert retained() is None


@pytest.mark.parametrize(
    ("left", "right", "valid"),
    [("", "", True), ("a" * 2048 + "b", "a" * 2048 + "c", False)],
)
def test_standard_schema_retains_empty_and_long_prefix_equality(
    left: str, right: str, valid: bool
) -> None:
    candidate = source({"const": right})

    result = validate_standard_schema(left, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"
