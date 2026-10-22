from dataclasses import dataclass
from typing import Protocol

from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.secret_value import SecretValue


@dataclass(frozen=True)
class SecretsCapabilities:
    pass


class SecretsProvider(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> SecretsCapabilities: ...

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue: ...

    async def list_names(self, prefix: str) -> list[str]: ...
