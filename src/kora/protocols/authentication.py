from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, cast

from pydantic import ConfigDict, StrictBool, field_validator, model_validator
from pydantic_core import PydanticCustomError

from kora.ir.json import JsonObject
from kora.observability import ComponentLogger
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier
from kora.protocols.time import Clock


class NamespaceGrant(BoundaryModel):
    model_config = ConfigDict(strict=True, revalidate_instances="always")

    namespace_id: Identifier
    transcripts: StrictBool


class Principal(BoundaryModel):
    model_config = ConfigDict(strict=True, revalidate_instances="always")

    token_id: Identifier
    kind: Literal["user", "service"]
    owner: Identifier
    namespaces: tuple[NamespaceGrant, ...]
    approve: StrictBool
    platform: StrictBool
    runtime: Identifier | None

    @field_validator("namespaces", mode="before")
    @classmethod
    def namespace_sequence(cls, value: object) -> object:
        return tuple(cast("list[object]", value)) if isinstance(value, list) else value

    @model_validator(mode="after")
    def consistent_authority(self) -> Self:
        if len({grant.namespace_id for grant in self.namespaces}) != len(
            self.namespaces
        ):
            raise PydanticCustomError(
                "principal_invalid", "namespace grants must be unique"
            )
        if self.kind == "user" and self.runtime is not None:
            raise PydanticCustomError(
                "principal_invalid", "a user principal has no runtime"
            )
        if self.kind == "service" and (
            self.runtime is None
            or self.approve
            or self.platform
            or any(grant.transcripts for grant in self.namespaces)
        ):
            raise PydanticCustomError(
                "principal_invalid", "a service principal has only worker authority"
            )
        return self


@dataclass(frozen=True, slots=True)
class RequestInfo:
    authorization: str | None = field(repr=False)
    requires_current_authority: bool = False

    def __post_init__(self) -> None:
        if self.authorization is not None and type(self.authorization) is not str:
            raise ValueError("the authorization header must be text or absent")
        if type(self.requires_current_authority) is not bool:
            raise ValueError("the current-authority requirement must be boolean")


@dataclass(frozen=True)
class AuthenticationContext:
    logger: ComponentLogger
    clock: Clock


@dataclass(frozen=True)
class AuthenticationCapabilities:
    pass


class Authenticator(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> AuthenticationCapabilities: ...

    async def authenticate(
        self, request: RequestInfo, ctx: AuthenticationContext
    ) -> Principal: ...

    async def revoke(self, token_id: str, ctx: AuthenticationContext) -> None: ...
