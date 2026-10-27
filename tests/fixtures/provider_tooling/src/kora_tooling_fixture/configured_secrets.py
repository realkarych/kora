import asyncio

from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.secret_value import SecretValue
from kora.protocols.secrets import SecretsCapabilities

REFERENCE: str = "secret://fixture/token"
REFUSED: EventSpec = CATALOG.register(
    EventSpec(
        "conformance.installed_fixture.refused",
        "conformance",
        Level.WARNING,
        "the installed fixture refused a reference",
        {"reason": FieldKind.STRING},
    )
)


def schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


class ConfiguredSecrets:
    name: str = "configured"

    def __init__(self, value: str, mode: str) -> None:
        self.value = value
        self.mode = mode
        self.config_schema = schema()
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        if mode == "construct":
            self.queue.put_nowait(None)
            raise ValueError("private-construction-marker")
        if mode == "schema":
            self.config_schema["title"] = "substituted"

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            completion.set_result(None)

    async def advance(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion

    def capabilities(self) -> SecretsCapabilities:
        return SecretsCapabilities()

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        await self.advance()
        admission = ctx.secret_admission
        if reference != REFERENCE or admission is None:
            ctx.logger.emit(REFUSED, reason="missing")
            message = reference if self.mode == "echo" else "no such secret"
            raise KoraProtocolError("secret_missing", message, False, {})
        return admission.admit(self.value)

    async def list_names(self, prefix: str) -> list[str]:
        await self.advance()
        return [prefix + "token"]

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.queue.put_nowait(None)
        await self.worker
        assert self.worker.done()


class WrappedSecrets:
    name: str = "configured"

    def __init__(self, provider: ConfiguredSecrets) -> None:
        self.provider = provider

    @property
    def config_schema(self) -> JsonObject:
        return self.provider.config_schema

    @property
    def value(self) -> str:
        return self.provider.value

    def capabilities(self) -> SecretsCapabilities:
        return self.provider.capabilities()

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        return await self.provider.resolve(reference, ctx)

    async def list_names(self, prefix: str) -> list[str]:
        return await self.provider.list_names(prefix)

    async def close(self) -> None:
        await self.provider.close()


def construct(value: str, mode: str) -> WrappedSecrets:
    return WrappedSecrets(ConfiguredSecrets(value, mode))


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.SECRETS, "configured", construct, schema(), "1"
    )
