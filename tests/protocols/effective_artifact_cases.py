from dataclasses import dataclass, field
from typing import cast

from kora.ir import ContractViolation
from kora.ir.json import JsonValue
from kora.ir.standard_output_preparation import StandardOutputInput
from kora.plugins.artifact_type.structured_output import (
    StructuredOutputArtifactType,
    effective_behavior_bindings,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.effective_artifact_type import (
    BehaviorBinding,
    EffectiveArtifactType,
    Role,
    SelectionBinding,
    SummaryBinding,
    ValidationBinding,
    prepare_effective_artifact_type,
)
from tests.conformance.artifact_types import SelectedFieldArtifactType
from tests.ir.standard_output_cases import output_input
from tests.plugins.artifact_type.structured_output_support import binding


@dataclass
class ObservedArtifactType(SelectedFieldArtifactType):
    calls: list[str] = field(default_factory=list[str])
    left_values: list[str] = field(default_factory=list[str])

    def validate(
        self, payload: JsonValue, ctx: ProtocolContext
    ) -> list[ContractViolation]:
        self.calls.append("validate")
        if isinstance(payload, dict) and isinstance(payload.get("left"), str):
            self.left_values.append(cast("str", payload["left"]))
        if self.defect == "raise":
            raise ValueError("private provider failure")
        return super().validate(payload, ctx)

    def digest_fields(self, payload: JsonValue, ctx: ProtocolContext) -> list[str]:
        self.calls.append("digest_fields")
        return super().digest_fields(payload, ctx)

    def summary(self, payload: JsonValue, ctx: ProtocolContext) -> JsonValue:
        self.calls.append("summary")
        return super().summary(payload, ctx)


def installed() -> dict[tuple[Role, str], BehaviorBinding]:
    return dict(
        effective_behavior_bindings(StructuredOutputArtifactType({}, binding()))
    )


def install_custom(
    bindings: dict[tuple[Role, str], BehaviorBinding],
    provider: SelectedFieldArtifactType,
    prefix: str = "example",
) -> None:
    bindings["validation", prefix + ".validation.v1"] = ValidationBinding(
        prefix + ".validation.v1", provider.validate
    )
    bindings["digest_fields", prefix + ".digest.v1"] = SelectionBinding(
        prefix + ".digest.v1", provider.digest_fields
    )
    bindings["summary", prefix + ".summary.v1"] = SummaryBinding(
        prefix + ".summary.v1", provider.summary
    )


def prepared(candidate: StandardOutputInput | None = None) -> EffectiveArtifactType:
    result = prepare_effective_artifact_type(
        output_input() if candidate is None else candidate, installed()
    )
    assert isinstance(result, EffectiveArtifactType)
    return result
