from copy import deepcopy
from typing import Final, Literal, cast

from kora.ir import ContractViolation
from kora.ir.data import detach_ir_data
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.ir.vcs_ref import (
    VcsRef,
    VcsRefValidationError,
    parse_vcs_ref,
    require_vcs_ref_revision,
    summarize_vcs_ref,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.artifact_type import (
    ArtifactTypeCapabilities,
    BuiltinArtifactTypeBinding,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata

CONFIG_SCHEMA: Final[JsonObject] = {"type": "object", "additionalProperties": False}
SUPPORTED_DESCRIPTOR: Final[JsonObject] = {
    "name": "VcsRef",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:VcsRef",
    "parent": None,
    "behavior": {
        "validation": "kora.vcs-ref-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_changed_paths": 10000,
        "max_changed_path_utf8_bytes": 524288,
        "max_revision_characters": 255,
        "max_principal_characters": 255,
        "max_json_depth": 16,
    },
    "digest_selection": [""],
    "schema_sha256": "6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8",
}
COMPONENT: Final[str] = CATALOG.register_component("artifact_type.vcs_ref")
OUTCOME: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "artifact_type.vcs_ref.outcome",
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


def matches_fixed(value: object, expected: JsonValue) -> bool:
    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        supplied = cast("dict[object, object]", value)
        target = cast("JsonObject", expected)
        if len(supplied) != len(target):
            return False
        if any(type(name) is not str for name in supplied):
            return False
        return all(
            name in supplied and matches_fixed(supplied[name], item)
            for name, item in target.items()
        )
    if type(expected) is list:
        supplied_items = cast("list[object]", value)
        target_items = cast("list[JsonValue]", expected)
        return len(supplied_items) == len(target_items) and all(
            matches_fixed(supplied_items[index], item)
            for index, item in enumerate(target_items)
        )
    return value == expected


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
    if type(binding.name) is not str or binding.name != "VcsRef":
        raise binding_error("name")
    if binding.parent is not None:
        raise binding_error("parent")
    catalogued_schema = builtin_schema("VcsRef", "1")
    if catalogued_schema is None:
        raise RuntimeError("the built-in VcsRef schema is unavailable")
    schema = cast("JsonObject", detach_ir_data(catalogued_schema))
    if not matches_fixed(binding.schema, schema):
        raise binding_error("schema")
    if not matches_fixed(binding.revision_descriptor, SUPPORTED_DESCRIPTOR):
        raise binding_error("revision_descriptor")
    compatible = True
    try:
        require_vcs_ref_revision(SUPPORTED_DESCRIPTOR)
    except VcsRefValidationError:
        compatible = False
    if not compatible:
        raise binding_error("revision_descriptor")
    return schema, deepcopy(SUPPORTED_DESCRIPTOR)


def parsed(payload: JsonValue) -> VcsRef | ContractViolation:
    try:
        return parse_vcs_ref(payload)
    except VcsRefValidationError as error:
        return error.violation


def report(ctx: ProtocolContext, operation: Operation, reason: str | None) -> None:
    ctx.logger.emit(
        OUTCOME,
        operation=operation,
        outcome="accepted" if reason is None else "refused",
        reason="valid" if reason is None else reason,
        violations=0 if reason is None else 1,
    )


class VcsRefArtifactType:
    name: str = "VcsRef"
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
    ) -> VcsRef:
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
        return summarize_vcs_ref(self._extract(payload, ctx, "summary"))


def create_provider(
    configuration: JsonObject, binding: BuiltinArtifactTypeBinding
) -> VcsRefArtifactType:
    return VcsRefArtifactType(configuration, binding)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_TYPE, "vcs_ref", create_provider, CONFIG_SCHEMA, "1"
    )
