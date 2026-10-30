from copy import deepcopy
from typing import Final, Literal, cast

from kora.ir import ContractViolation
from kora.ir.data import detach_ir_data
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    parse_structured_output,
    require_structured_output_revision,
    summarize_structured_output,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.artifact_type.binding import matches_fixed
from kora.protocols.artifact_type import (
    ArtifactTypeCapabilities,
    BuiltinArtifactTypeBinding,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata

CONFIG_SCHEMA: Final[JsonObject] = {"type": "object", "additionalProperties": False}
SUPPORTED_DESCRIPTOR: Final[JsonObject] = {
    "name": "StructuredOutput",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:StructuredOutput",
    "parent": None,
    "behavior": {
        "validation": "kora.structured-output-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_json_depth": 16,
        "max_integer_bits": 14000,
    },
    "canonicalization": "kora.structured-json.v1",
    "compatibility": {
        "execution_semantics": "kora.execution.v1",
        "checker_view": "kora.structured-output-view.v1",
    },
    "digest_selection": [""],
    "schema_sha256": "e924b9a44c0825f0b2a43b0291d1f98c4851425a50e291914dd85ba096ac6eae",
}
COMPONENT: Final[str] = CATALOG.register_component("artifact_type.structured_output")
OUTCOME: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "artifact_type.structured_output.outcome",
        COMPONENT,
        Level.DEBUG,
        "an artifact type operation completed",
        {
            "operation": FieldKind.STRING,
            "outcome": FieldKind.STRING,
            "reason": FieldKind.STRING,
            "violations": FieldKind.INTEGER,
        },
    )
)
Operation = Literal["validate", "digest_fields", "summary"]


def binding_error(rule: str) -> KoraProtocolError:
    return KoraProtocolError(
        "artifact_type_binding_incompatible",
        "the artifact type binding is incompatible",
        False,
        {"rule": rule},
    )


def checked_binding(
    binding: BuiltinArtifactTypeBinding,
) -> tuple[JsonObject, JsonObject]:
    if type(binding) is not BuiltinArtifactTypeBinding:
        raise binding_error("binding")
    if type(binding.name) is not str or binding.name != "StructuredOutput":
        raise binding_error("name")
    if binding.parent is not None:
        raise binding_error("parent")
    catalogued_schema = builtin_schema("StructuredOutput", "1")
    if catalogued_schema is None:
        raise RuntimeError("the built-in StructuredOutput schema is unavailable")
    schema = cast("JsonObject", detach_ir_data(catalogued_schema))
    if not matches_fixed(binding.schema, schema):
        raise binding_error("schema")
    if not matches_fixed(binding.revision_descriptor, SUPPORTED_DESCRIPTOR):
        raise binding_error("revision_descriptor")
    compatible = True
    try:
        require_structured_output_revision(SUPPORTED_DESCRIPTOR)
    except StructuredOutputValidationError:
        compatible = False
    if not compatible:
        raise binding_error("revision_descriptor")
    return schema, deepcopy(SUPPORTED_DESCRIPTOR)


def parsed(payload: JsonValue) -> StructuredOutput | ContractViolation:
    try:
        return parse_structured_output(payload)
    except StructuredOutputValidationError as error:
        return error.violation


def report(ctx: ProtocolContext, operation: Operation, reason: str | None) -> None:
    ctx.logger.emit(
        OUTCOME,
        operation=operation,
        outcome="accepted" if reason is None else "refused",
        reason="valid" if reason is None else reason,
        violations=0 if reason is None else 1,
    )


class StructuredOutputArtifactType:
    name: str = "StructuredOutput"
    parent: str | None = None

    def __init__(
        self, configuration: JsonObject, binding: BuiltinArtifactTypeBinding
    ) -> None:
        if type(configuration) is not dict or configuration:
            raise KoraProtocolError(
                "artifact_type_configuration_invalid",
                "the artifact type configuration is invalid",
                False,
                {"rule": "closed_empty_object"},
            )
        schema, revision_descriptor = checked_binding(binding)
        self._schema: JsonObject = schema
        self._revision_descriptor: JsonObject = revision_descriptor
        self.config_schema: JsonObject = deepcopy(CONFIG_SCHEMA)

    @property
    def schema(self) -> JsonObject:
        return deepcopy(self._schema)

    def capabilities(self) -> ArtifactTypeCapabilities:
        return ArtifactTypeCapabilities()

    def validate(
        self, payload: JsonValue, ctx: ProtocolContext
    ) -> list[ContractViolation]:
        outcome = parsed(payload)
        violation = outcome if isinstance(outcome, ContractViolation) else None
        report(ctx, "validate", None if violation is None else violation.code)
        return [] if violation is None else [violation]

    def _extract(
        self, payload: JsonValue, ctx: ProtocolContext, operation: Operation
    ) -> StructuredOutput:
        outcome = parsed(payload)
        reason = outcome.code if isinstance(outcome, ContractViolation) else None
        report(ctx, operation, reason)
        if isinstance(outcome, ContractViolation):
            raise KoraProtocolError(
                "artifact_type_payload_invalid",
                "the artifact type payload is invalid",
                False,
                {"violation": outcome.code},
            )
        return outcome

    def digest_fields(self, payload: JsonValue, ctx: ProtocolContext) -> list[str]:
        self._extract(payload, ctx, "digest_fields")
        return [""]

    def summary(self, payload: JsonValue, ctx: ProtocolContext) -> JsonValue:
        return summarize_structured_output(self._extract(payload, ctx, "summary"))


def create_provider(
    configuration: JsonObject, binding: BuiltinArtifactTypeBinding
) -> StructuredOutputArtifactType:
    return StructuredOutputArtifactType(configuration, binding)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_TYPE,
        "structured_output",
        create_provider,
        CONFIG_SCHEMA,
        "1",
    )
