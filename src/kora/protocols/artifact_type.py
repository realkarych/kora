from dataclasses import dataclass
from typing import Protocol

from kora.ir import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.common import ProtocolContext


@dataclass(frozen=True)
class BuiltinArtifactTypeBinding:
    name: str
    schema: JsonObject
    parent: str | None
    revision_descriptor: JsonObject


@dataclass(frozen=True)
class ArtifactTypeCapabilities:
    pass


class ArtifactType(Protocol):
    name: str
    parent: str | None
    config_schema: JsonObject

    @property
    def schema(self) -> JsonObject: ...

    def capabilities(self) -> ArtifactTypeCapabilities: ...

    def validate(
        self, payload: JsonValue, ctx: ProtocolContext
    ) -> list[ContractViolation]: ...

    def digest_fields(self, payload: JsonValue, ctx: ProtocolContext) -> list[str]: ...

    def summary(self, payload: JsonValue, ctx: ProtocolContext) -> JsonValue: ...
