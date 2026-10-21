import enum
import re
from collections.abc import Callable
from dataclasses import dataclass

from kora.protocols.json import (
    JSON_RULES,
    JsonObject,
    JsonShapeError,
    parse_json_object,
)

PROVIDER_NAME: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]{0,62}")
VERSION: re.Pattern[str] = re.compile(r"[\x20-\x7e]{1,64}")
CONFIG_SCHEMA_MAX_DEPTH: int = 16
METADATA_RULES: frozenset[str] = frozenset(
    {"protocol", "name", "implementation", "version", "config_schema.type"}
    | {f"config_schema.{rule}" for rule in JSON_RULES}
)


class ProtocolName(enum.StrEnum):
    RUNTIME = "runtime"
    HARNESS = "harness"
    VCS = "vcs"
    APPROVAL_CHANNEL = "approval_channel"
    SECRETS = "secrets"
    ARTIFACT_STORAGE = "artifact_storage"
    ARTIFACT_TYPE = "artifact_type"
    OBSERVABILITY_EXPORTER = "observability_exporter"
    AUTHENTICATION = "authentication"
    SENSOR = "sensor"
    POLICY = "policy"


class ProviderMetadataError(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def is_provider_name(value: object) -> bool:
    return isinstance(value, str) and PROVIDER_NAME.fullmatch(value) is not None


def parse_protocol_name(value: str) -> ProtocolName | None:
    try:
        return ProtocolName(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ProviderMetadata:
    protocol: ProtocolName
    name: str
    implementation: Callable[..., object]
    config_schema: JsonObject
    version: str

    def __post_init__(self) -> None:
        require(isinstance_of(self.protocol, ProtocolName), "protocol")
        require(is_provider_name(self.name), "name")
        require(is_callable(self.implementation), "implementation")
        object.__setattr__(
            self, "config_schema", parse_config_schema(self.config_schema)
        )
        require(is_version(self.version), "version")


def require(condition: bool, rule: str) -> None:
    if not condition:
        raise ProviderMetadataError(rule)


def isinstance_of(value: object, kind: type) -> bool:
    return isinstance(value, kind)


def is_callable(value: object) -> bool:
    return callable(value)


def is_version(value: object) -> bool:
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


def parse_config_schema(value: object) -> JsonObject:
    try:
        schema = parse_json_object(value, CONFIG_SCHEMA_MAX_DEPTH)
    except JsonShapeError as error:
        raise ProviderMetadataError(f"config_schema.{error.rule}") from None
    if schema.get("type") != "object":
        raise ProviderMetadataError("config_schema.type")
    return schema
