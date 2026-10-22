from dataclasses import dataclass
from typing import Final

from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretValue
from kora.protocols.secrets import SecretsCapabilities

REFERENCE: Final[str] = "secret://test/token"
MISSING: Final[str] = "secret://test/absent"
VALUE: Final[str] = 'conformance-token-"quoted"-é'
RESOLVE_FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "conformance.test_provider.resolve_failed",
        CONFORMANCE_COMPONENT,
        Level.WARNING,
        "a test secret reference was not found",
        {"reason": FieldKind.STRING},
    )
)

RESOLVING: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "conformance.test_provider.resolving",
        CONFORMANCE_COMPONENT,
        Level.INFO,
        "a test provider is resolving a secret",
        {"detail": FieldKind.STRING},
    )
)


class ReferenceSecretsProvider:
    name: str = "test"
    config_schema: JsonObject = {"type": "object"}

    def capabilities(self) -> SecretsCapabilities:
        return SecretsCapabilities()

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        admission = ctx.secret_admission
        if reference != REFERENCE or admission is None:
            ctx.logger.emit(RESOLVE_FAILED, reason="not_found")
            raise KoraProtocolError("secret_not_found", "no such secret", False, {})
        return admission.admit(VALUE)

    async def list_names(self, prefix: str) -> list[str]:
        return ["token"] if prefix == "" else []


class EchoingSecretsProvider(ReferenceSecretsProvider):
    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        raise KoraProtocolError(
            "secret_not_found", f"no such secret {reference}", True, {"ref": reference}
        )


class WrongValueSecretsProvider(ReferenceSecretsProvider):
    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        admission = ctx.secret_admission
        if admission is None:
            raise KoraProtocolError("secret_not_found", "no such secret", False, {})
        return admission.admit(f"other-{VALUE}")


class LeakingSecretsProvider(ReferenceSecretsProvider):
    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        ctx.logger.emit(RESOLVING, detail=VALUE)
        return await super().resolve(reference, ctx)


@dataclass
class MutableCapabilities:
    calls: int


class MutableCapabilitiesProvider:
    name: str = "mutable"
    config_schema: JsonObject = {"type": "array"}

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> MutableCapabilities:
        self.calls += 1
        return MutableCapabilities(self.calls)


async def exercise(ctx: ProtocolContext, marker: str) -> object:
    return await ReferenceSecretsProvider().resolve(f"{MISSING}/{marker}", ctx)


async def echoing_exercise(ctx: ProtocolContext, marker: str) -> object:
    return await EchoingSecretsProvider().resolve(f"{MISSING}/{marker}", ctx)


async def silent_exercise(ctx: ProtocolContext, marker: str) -> object:
    raise KoraProtocolError("secret_not_found", "no such secret", False, {})
