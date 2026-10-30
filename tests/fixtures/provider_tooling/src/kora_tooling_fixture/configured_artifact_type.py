from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.artifact_type import ArtifactTypeCapabilities
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata

OUTCOME: EventSpec = CATALOG.register(
    EventSpec(
        "conformance.configured_artifact_type.outcome",
        "conformance",
        Level.DEBUG,
        "the configured artifact fixture completed an operation",
        {"operation": FieldKind.STRING, "outcome": FieldKind.STRING},
    )
)
VIOLATIONS: tuple[ContractViolation, ...] = (
    ContractViolation(
        code="FIX001", message="The left field is invalid.", path=("left",)
    ),
    ContractViolation(
        code="FIX002", message="The right field is invalid.", path=("right",)
    ),
)


def schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
        "required": ["left", "right"],
        "additionalProperties": False,
    }


def configuration_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {"selected": {"enum": ["left", "right"]}},
        "required": ["selected"],
        "additionalProperties": False,
    }


class ConfiguredArtifactType:
    name: str = "ConfiguredArtifact"
    parent: str | None = None
    config_schema: JsonObject = configuration_schema()

    def __init__(self, configuration: JsonObject) -> None:
        assert set(configuration) == {"selected"}
        selected = configuration["selected"]
        assert selected in {"left", "right"}
        assert isinstance(selected, str)
        self.selected = selected

    @property
    def schema(self) -> JsonObject:
        return schema()

    def capabilities(self) -> ArtifactTypeCapabilities:
        return ArtifactTypeCapabilities()

    def validate(
        self, payload: JsonValue, ctx: ProtocolContext
    ) -> list[ContractViolation]:
        valid = (
            isinstance(payload, dict)
            and set(payload) == {"left", "right"}
            and isinstance(payload["left"], str)
            and isinstance(payload["right"], str)
            and payload["left"] != payload["right"]
        )
        ctx.logger.emit(
            OUTCOME, operation="validate", outcome="accepted" if valid else "refused"
        )
        return [] if valid else list(VIOLATIONS)

    def require(self, payload: JsonValue, ctx: ProtocolContext) -> JsonObject:
        if self.validate(payload, ctx):
            raise KoraProtocolError(
                "fixture_payload_invalid", "The fixture payload is invalid.", False, {}
            )
        assert isinstance(payload, dict)
        return payload

    def digest_fields(self, payload: JsonValue, ctx: ProtocolContext) -> list[str]:
        self.require(payload, ctx)
        return ["/" + self.selected]

    def summary(self, payload: JsonValue, ctx: ProtocolContext) -> JsonValue:
        return self.require(payload, ctx)[self.selected]


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_TYPE,
        "configured",
        ConfiguredArtifactType,
        configuration_schema(),
        "1",
    )
