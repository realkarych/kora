from copy import deepcopy
from typing import Final, Literal, cast

from kora.ir import ContractViolation
from kora.ir.command_result import (
    CommandResult,
    CommandResultValidationError,
    parse_command_result,
    require_command_result_revision,
    summarize_command_result,
)
from kora.ir.data import detach_ir_data
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
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
    "name": "CommandResult",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:CommandResult",
    "parent": None,
    "behavior": {
        "validation": "kora.command-result-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_json_depth": 16,
        "max_count": 9223372036854775807,
        "max_capture_bytes": 67108864,
        "max_tail_bytes": 4096,
        "max_resources": 64,
        "max_resource_bytes": 262144,
        "max_resource_name_utf8_bytes": 4096,
        "max_resource_component_utf8_bytes": 255,
        "max_protected_paths": 64,
        "max_protected_path_characters": 4096,
        "max_protected_path_segments": 32,
        "max_protected_path_pieces": 8192,
        "max_protected_path_utf8_bytes": 65536,
        "max_protected_path_non_ascii_characters": 16384,
        "max_repo_utf8_bytes": 4096,
        "max_revision_characters": 255,
    },
    "canonicalization": "kora.command-result-json.v1",
    "digest_selection": [""],
    "schema_sha256": "8444eeb4d4688b6d059dd7221689dd4c824084c88ff189ee3fbb848153c1f8d4",
}
COMPONENT: Final[str] = CATALOG.register_component("artifact_type.command_result")
OUTCOME: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "artifact_type.command_result.outcome",
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
    if type(binding.name) is not str or binding.name != "CommandResult":
        raise binding_error("name")
    if binding.parent is not None:
        raise binding_error("parent")
    catalogued_schema = builtin_schema("CommandResult", "1")
    if catalogued_schema is None:
        raise RuntimeError("the built-in CommandResult schema is unavailable")
    schema = cast("JsonObject", detach_ir_data(catalogued_schema))
    if not matches_fixed(binding.schema, schema):
        raise binding_error("schema")
    if not matches_fixed(binding.revision_descriptor, SUPPORTED_DESCRIPTOR):
        raise binding_error("revision_descriptor")
    compatible = True
    try:
        require_command_result_revision(SUPPORTED_DESCRIPTOR)
    except CommandResultValidationError:
        compatible = False
    if not compatible:
        raise binding_error("revision_descriptor")
    return schema, deepcopy(SUPPORTED_DESCRIPTOR)


def parsed(payload: JsonValue) -> CommandResult | ContractViolation:
    try:
        return parse_command_result(payload)
    except CommandResultValidationError as error:
        return error.violation


def report(ctx: ProtocolContext, operation: Operation, reason: str | None) -> None:
    ctx.logger.emit(
        OUTCOME,
        operation=operation,
        outcome="accepted" if reason is None else "refused",
        reason="valid" if reason is None else reason,
        violations=0 if reason is None else 1,
    )


class CommandResultArtifactType:
    name: str = "CommandResult"
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
    ) -> CommandResult:
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
        return summarize_command_result(self._extract(payload, ctx, "summary"))


def create_provider(
    configuration: JsonObject, binding: BuiltinArtifactTypeBinding
) -> CommandResultArtifactType:
    return CommandResultArtifactType(configuration, binding)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_TYPE,
        "command_result",
        create_provider,
        CONFIG_SCHEMA,
        "1",
    )
