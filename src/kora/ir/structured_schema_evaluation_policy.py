from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Never, cast

Reason = Literal["unsupported_policy", "unsupported_schema", "evaluation_exhausted"]
PolicyIdentity = Literal["kora.structured-output-schema-policy.v1"]
POLICY: PolicyIdentity = "kora.structured-output-schema-policy.v1"
PARAMETERS: Mapping[str, int] = MappingProxyType(
    {
        "max_input_bytes": 1048576,
        "max_input_nodes": 16384,
        "max_input_depth": 32,
        "max_schema_documents": 64,
        "max_schema_positions": 2048,
        "max_reference_edges": 4096,
        "max_numeric_bits": 14000,
        "max_preparation_work": 16777216,
        "max_auxiliary_bytes": 4194304,
        "max_auxiliary_entries": 32768,
        "max_numeric_intermediate_bits": 16384,
        "max_evaluation_work": 16777216,
        "max_evaluation_depth": 64,
    }
)


class EvaluationRefused(Exception):
    def __init__(self, reason: Reason) -> None:
        if type(reason) is not str or reason not in (
            "unsupported_policy",
            "unsupported_schema",
            "evaluation_exhausted",
        ):
            raise ValueError("invalid evaluation refusal")
        super().__init__(reason)
        self.reason: Reason = reason


def refuse(reason: Reason = "unsupported_schema") -> Never:
    raise EvaluationRefused(reason) from None


def _record(value: object, names: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:
        refuse("unsupported_policy")
    mapping = cast("dict[object, object]", value)
    if len(mapping) != len(names):
        refuse("unsupported_policy")
    for name in mapping:
        if type(name) is not str or not any(
            len(name) == len(expected) and name == expected for expected in names
        ):
            refuse("unsupported_policy")
    return cast("dict[str, object]", mapping)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class StructuredSchemaEvaluationPolicy:
    policy: PolicyIdentity
    parameters: Mapping[str, int]

    def __init__(self, value: object) -> None:
        binding = _record(value, ("policy", "parameters"))
        identity = binding["policy"]
        if (
            type(identity) is not str
            or len(identity) != len(POLICY)
            or identity != POLICY
        ):
            refuse("unsupported_policy")
        parameters = _record(binding["parameters"], tuple(PARAMETERS))
        for name, expected in PARAMETERS.items():
            current = parameters[name]
            if type(current) is not int or current != expected:
                refuse("unsupported_policy")
        object.__setattr__(self, "policy", POLICY)
        object.__setattr__(self, "parameters", PARAMETERS)

    def __repr__(self) -> str:
        return "StructuredSchemaEvaluationPolicy()"

    def preparation_parameters(self) -> dict[str, int]:
        return {
            "max_input_bytes": PARAMETERS["max_input_bytes"],
            "max_input_nodes": PARAMETERS["max_input_nodes"],
            "max_input_depth": PARAMETERS["max_input_depth"],
            "max_schema_documents": PARAMETERS["max_schema_documents"],
            "max_schema_positions": PARAMETERS["max_schema_positions"],
            "max_reference_edges": PARAMETERS["max_reference_edges"],
            "max_numeric_bits": PARAMETERS["max_numeric_bits"],
            "max_preparation_work": PARAMETERS["max_preparation_work"],
            "max_auxiliary_bytes": PARAMETERS["max_auxiliary_bytes"],
            "max_auxiliary_entries": PARAMETERS["max_auxiliary_entries"],
        }


def bind_evaluation_policy(value: object) -> StructuredSchemaEvaluationPolicy:
    return StructuredSchemaEvaluationPolicy(value)
