from copy import deepcopy
from dataclasses import dataclass, field

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.artifact_type import ArtifactType, ArtifactTypeCapabilities
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError

VIOLATIONS: tuple[ContractViolation, ...] = (
    ContractViolation(
        code="FIELD001", message="The left field is invalid.", path=("left",)
    ),
    ContractViolation(
        code="FIELD002", message="The right field is invalid.", path=("right",)
    ),
)


@dataclass
class SelectedFieldArtifactType:
    selected: str = "right"
    defect: str = "none"
    name: str = "SelectedField"
    parent: str | None = None
    config_schema: JsonObject = field(default_factory=lambda: {"type": "object"})
    retained_selection: list[str] | None = None
    retained_summary: JsonValue = None

    @property
    def schema(self) -> JsonObject:
        return {"type": "object"}

    def capabilities(self) -> ArtifactTypeCapabilities:
        return ArtifactTypeCapabilities()

    def validate(
        self, payload: JsonValue, ctx: ProtocolContext
    ) -> list[ContractViolation]:
        if self.defect == "accept_invalid":
            return []
        if not isinstance(payload, dict) or set(payload) != {"left", "right"}:
            return list(VIOLATIONS)
        if self.defect == "mutate_validation_input":
            payload["left"] = "changed"
        return []

    def require(self, payload: JsonValue, ctx: ProtocolContext) -> JsonObject:
        if self.validate(payload, ctx) and self.defect != "extract_invalid":
            raise KoraProtocolError(
                "selected_payload_invalid", "The payload is invalid.", False, {}
            )
        return payload if isinstance(payload, dict) else {}

    def digest_fields(self, payload: JsonValue, ctx: ProtocolContext) -> list[str]:
        value = self.require(payload, ctx)
        if self.defect == "mutate_selection_input":
            value["left"] = "changed"
        if self.defect == "reuse_selection":
            if self.retained_selection is None:
                self.retained_selection = ["/" + self.selected]
            return self.retained_selection
        return [""] if self.defect == "wrong_selection" else ["/" + self.selected]

    def summary(self, payload: JsonValue, ctx: ProtocolContext) -> JsonValue:
        value = self.require(payload, ctx)
        if self.defect == "wrong_summary":
            return value
        if self.defect == "mutate_summary_input":
            value["left"] = "changed"
        selected = value.get(self.selected)
        if self.defect == "alias_summary":
            return selected
        if self.defect == "reuse_summary":
            if self.retained_summary is None:
                self.retained_summary = deepcopy(selected)
            return self.retained_summary
        return deepcopy(selected)


def payload() -> JsonValue:
    return {"left": "first", "right": {"nested": ["second"]}}


def equivalent() -> JsonValue:
    return {"right": {"nested": ["second"]}, "left": "first"}


def rejected(marker: str) -> JsonValue:
    return {"invalid": marker}


async def failure(
    provider: ArtifactType, configuration: JsonObject, ctx: ProtocolContext, marker: str
) -> object:
    return provider.summary(rejected(marker), ctx)


async def effect(
    provider: ArtifactType, configuration: JsonObject, ctx: ProtocolContext
) -> None:
    assert provider.digest_fields(payload(), ctx) == [
        "/" + str(configuration["selected"])
    ]


def inputs() -> ArtifactTypeConformanceInputs:
    return ArtifactTypeConformanceInputs(
        (
            ArtifactTypeConformanceCase(
                payload, equivalent, ("/right",), lambda: {"nested": ["second"]}
            ),
        ),
        rejected,
        VIOLATIONS,
        failure,
        effect,
    )
