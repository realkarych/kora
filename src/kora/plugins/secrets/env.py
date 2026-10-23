import os
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final, NoReturn, Self

from pydantic import StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from kora.ir.json import JsonObject, parse_json_object
from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    SecretAdmissionError,
)
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Identifier
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.secret_value import SecretAdmission, SecretValue
from kora.protocols.secrets import SecretsCapabilities

NAME_BODY: Final[str] = r"[A-Z_][A-Z0-9_]{0,127}"
ENV_NAME_PATTERN: Final[str] = f"^{NAME_BODY}$"
NAME: Final[re.Pattern[str]] = re.compile(NAME_BODY)
REFERENCE: Final[re.Pattern[str]] = re.compile(f"secret://env/({NAME_BODY})")
RESERVED_FAMILIES: Final[tuple[str, ...]] = (
    "KORA_",
    "AWS_",
    "GITHUB_",
    "GH_",
    "SSH_",
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "CODEX_",
    "GOOGLE_",
    "AZURE_",
)
MAX_LISTED_NAMES: Final[int] = 1000
VERSION: Final[str] = "1"
REFUSALS: Final[dict[str, str]] = {
    "secret_reference_invalid": "the secret reference is invalid",
    "secret_reference_not_allowed": "the secret reference is not allowed",
    "secret_resolution_unsealed": "secrets resolve only after the request is sealed",
    "secret_admission_missing": "the call has no secret admission",
    "secret_missing": "the secret is not set",
    "secret_admission_refused": "the secret was refused admission",
}

COMPONENT: Final[str] = CATALOG.register_component("secrets.env")
RESOLVED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "secrets.env.resolved",
        COMPONENT,
        Level.INFO,
        "a secret reference was resolved and admitted",
        {},
    )
)
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "secrets.env.refused",
        COMPONENT,
        Level.WARNING,
        "a secret reference was refused",
        {"reason": FieldKind.STRING},
    )
)

EnvName = Annotated[str, StringConstraints(strict=True, pattern=ENV_NAME_PATTERN)]


def overlaps_reserved(prefix: str) -> bool:
    return any(
        prefix.startswith(family) or family.startswith(prefix)
        for family in RESERVED_FAMILIES
    )


class EnvSecretsConfiguration(BoundaryModel):
    namespace_prefixes: dict[Identifier, tuple[EnvName, ...]]
    excluded_names: tuple[EnvName, ...] = ()

    @model_validator(mode="after")
    def refuse_unsafe_prefixes(self) -> Self:
        for prefixes in self.namespace_prefixes.values():
            for prefix in prefixes:
                if overlaps_reserved(prefix) or any(
                    name.startswith(prefix) for name in self.excluded_names
                ):
                    raise PydanticCustomError(
                        "secret_prefix_reserved",
                        "a prefix overlaps a reserved or excluded name",
                    )
        return self


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    EnvSecretsConfiguration.model_json_schema()
)


def refused(ctx: ProtocolContext, code: str) -> NoReturn:
    ctx.logger.emit(REFUSED, reason=code)
    raise KoraProtocolError(code, REFUSALS[code], False, {})


def admitted(admission: SecretAdmission, value: str) -> SecretValue | None:
    try:
        return admission.admit(value)
    except SecretAdmissionError:
        return None


class EnvSecretsProvider:
    name: str = "env"
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(
        self, configuration: EnvSecretsConfiguration, environment: Mapping[str, str]
    ) -> None:
        self._prefixes: Mapping[str, tuple[str, ...]] = MappingProxyType(
            dict(configuration.namespace_prefixes)
        )
        self._excluded = frozenset(configuration.excluded_names)
        self._listed = tuple(
            sorted({prefix for group in self._prefixes.values() for prefix in group})
        )
        self._environment = environment

    def capabilities(self) -> SecretsCapabilities:
        return SecretsCapabilities()

    def forbidden(self, name: str) -> bool:
        reserved = any(name.startswith(family) for family in RESERVED_FAMILIES)
        return reserved or name in self._excluded

    def readable(self, namespace: str, name: str) -> bool:
        if self.forbidden(name):
            return False
        return any(
            name.startswith(prefix) for prefix in self._prefixes.get(namespace, ())
        )

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        match = REFERENCE.fullmatch(reference)
        if match is None:
            refused(ctx, "secret_reference_invalid")
        name = match[1]
        if not self.readable(ctx.namespace_id, name):
            refused(ctx, "secret_reference_not_allowed")
        if ctx.request_digest is None:
            refused(ctx, "secret_resolution_unsealed")
        admission = ctx.secret_admission
        if admission is None:
            refused(ctx, "secret_admission_missing")
        value = self._environment.get(name)
        if value is None:
            refused(ctx, "secret_missing")
        secret = admitted(admission, value)
        if secret is None:
            refused(ctx, "secret_admission_refused")
        ctx.logger.emit(RESOLVED)
        return secret

    async def list_names(self, prefix: str) -> list[str]:
        if not any(prefix.startswith(listed) for listed in self._listed):
            return []
        names = sorted(
            name
            for name in self._environment
            if name.startswith(prefix)
            and NAME.fullmatch(name) is not None
            and not self.forbidden(name)
        )
        return names[:MAX_LISTED_NAMES]


def create_provider(configuration: JsonObject) -> EnvSecretsProvider:
    parsed = EnvSecretsConfiguration.model_validate(configuration)
    return EnvSecretsProvider(parsed, os.environ)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.SECRETS, "env", create_provider, CONFIG_SCHEMA, VERSION
    )
