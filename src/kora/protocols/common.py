from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from kora.ir.json import JsonObject
from kora.observability import ComponentLogger
from kora.observability.policy import CONTEXT_IDENTIFIER
from kora.protocols.activity import ActivityReporter
from kora.protocols.runtime import ExecutionDeadline
from kora.protocols.secret_value import SecretAdmission, SecretScope, SecretValue
from kora.protocols.time import Clock


class BoundaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class SecretResolver(Protocol):
    async def resolve(self, reference: str, scope: SecretScope) -> SecretValue: ...


class ProviderProtocol(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> object: ...


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and CONTEXT_IDENTIFIER.match(value) is not None


def is_optional_identifier(value: object) -> bool:
    return value is None or is_identifier(value)


@dataclass(frozen=True)
class ProtocolContext:
    namespace_id: str
    run_id: str | None
    attempt_id: str | None
    logger: ComponentLogger
    secrets: SecretResolver | None
    secret_admission: SecretAdmission | None
    request_digest: str | None
    deadline: ExecutionDeadline | None
    clock: Clock
    activity: ActivityReporter

    def __post_init__(self) -> None:
        if not is_identifier(self.namespace_id):
            raise ValueError("namespace_id is a bounded identifier")
        for name in ("run_id", "attempt_id", "request_digest"):
            if not is_optional_identifier(getattr(self, name)):
                raise ValueError(f"{name} is None or a bounded identifier")
