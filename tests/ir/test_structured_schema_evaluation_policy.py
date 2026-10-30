from dataclasses import FrozenInstanceError, replace
from typing import Never

import pytest

from kora.ir.structured_schema_evaluation_policy import (
    PARAMETERS,
    POLICY,
    EvaluationRefused,
    bind_evaluation_policy,
)
from tests.ir.structured_schema_cases import parameters, preparation_input, prepared


def complete_binding() -> dict[str, object]:
    return {"policy": POLICY, "parameters": dict(PARAMETERS)}


class AuthoredInteger(int):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("authored parameter equality invoked")


class AuthoredString(str):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("authored policy equality invoked")

    __hash__ = str.__hash__


class AuthoredDictionary(dict[str, object]):
    def items(self) -> Never:
        raise AssertionError("authored binding traversal invoked")


def test_structured_schema_policy_pins_all_thirteen_parameters() -> None:
    expected = parameters() | {
        "max_numeric_intermediate_bits": 16_384,
        "max_evaluation_work": 16_777_216,
        "max_evaluation_depth": 64,
    }

    binding = bind_evaluation_policy(complete_binding())

    assert POLICY == "kora.structured-output-schema-policy.v1"
    assert dict(PARAMETERS) == expected
    assert len(PARAMETERS) == 13
    assert binding.preparation_parameters() == parameters()


def test_structured_schema_policy_projection_preserves_preparation_acceptance() -> None:
    candidate = complete_binding()
    binding = bind_evaluation_policy(candidate)
    projection = binding.preparation_parameters()
    baseline_input = preparation_input({"multipleOf": 0.1})

    baseline = prepared(baseline_input)
    projected = prepared(replace(baseline_input, preparation_parameters=projection))
    projection.clear()
    candidate.clear()

    assert projected == baseline
    assert binding.preparation_parameters() == parameters()
    assert len(binding.preparation_parameters()) == 10


def test_structured_schema_policy_binding_is_immutable() -> None:
    binding = bind_evaluation_policy(complete_binding())
    attribute = "policy"

    with pytest.raises(FrozenInstanceError):
        setattr(binding, attribute, "authored-policy-canary")

    assert "authored-policy-canary" not in repr(binding)


@pytest.mark.parametrize(
    ("key", "change"),
    [(key, "missing") for key in PARAMETERS]
    + [
        ("max_input_bytes", "wrong"),
        ("max_evaluation_work", "wrong"),
        ("max_input_bytes", "boolean"),
        ("max_evaluation_work", "boolean"),
        ("max_input_bytes", "subclass"),
        ("max_evaluation_work", "subclass"),
    ],
)
def test_structured_schema_policy_refuses_incomplete_or_inexact_parameters(
    key: str, change: str
) -> None:
    values: dict[str, object] = dict(PARAMETERS)
    if change == "missing":
        del values[key]
    elif change == "wrong":
        values[key] = PARAMETERS[key] + 1
    elif change == "boolean":
        values[key] = True
    else:
        values[key] = AuthoredInteger(PARAMETERS[key])
    candidate = {"policy": POLICY, "parameters": values}

    with pytest.raises(EvaluationRefused) as rejected:
        bind_evaluation_policy(candidate)

    assert rejected.value.reason == "unsupported_policy"


@pytest.mark.parametrize(
    "change",
    [
        "missing-policy",
        "missing-parameters",
        "extra-binding-key",
        "extra-parameter",
        "unknown-policy",
        "policy-subclass",
        "key-subclass",
        "binding-subclass",
        "parameters-subclass",
    ],
)
def test_structured_schema_policy_requires_exact_native_closed_binding(
    change: str,
) -> None:
    candidate: object = complete_binding()
    values: dict[str, object] = dict(PARAMETERS)
    native: dict[str, object] = {"policy": POLICY, "parameters": values}
    if change == "missing-policy":
        del native["policy"]
    elif change == "missing-parameters":
        del native["parameters"]
    elif change == "extra-binding-key":
        native["authored-binding-canary"] = 0
    elif change == "extra-parameter":
        values["authored-parameter-canary"] = 0
    elif change == "unknown-policy":
        native["policy"] = "authored-policy-canary"
    elif change == "policy-subclass":
        native["policy"] = AuthoredString(POLICY)
    elif change == "key-subclass":
        values = {
            AuthoredString(key) if key == "max_input_bytes" else key: value
            for key, value in PARAMETERS.items()
        }
        native["parameters"] = values
    elif change == "binding-subclass":
        candidate = AuthoredDictionary(native)
    else:
        native["parameters"] = AuthoredDictionary(values)
    if change != "binding-subclass":
        candidate = native

    with pytest.raises(EvaluationRefused) as rejected:
        bind_evaluation_policy(candidate)

    assert rejected.value.reason == "unsupported_policy"
    assert "authored" not in str(rejected.value)
    assert "authored" not in repr(rejected.value)


def test_structured_schema_policy_checks_kinds_without_metaclass_callbacks() -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("equality")
            raise AssertionError("authored metaclass equality invoked")

    class Authored(metaclass=AuthoredMeta):
        def __getattr__(self, name: str) -> object:
            calls.append("attribute")
            raise AssertionError("authored binding attribute invoked")

    candidate = Authored()

    with pytest.raises(EvaluationRefused) as rejected:
        bind_evaluation_policy(candidate)

    assert rejected.value.reason == "unsupported_policy"
    assert calls == []
