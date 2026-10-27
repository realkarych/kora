import asyncio
from collections.abc import Callable

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.common import MARKER
from kora.conformance.secrets import SecretsConformanceInputs
from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.secrets import SecretsProvider
from kora_tooling_fixture.configured_secrets import REFERENCE, WrappedSecrets


async def failure(
    provider: SecretsProvider,
    configuration: JsonObject,
    context: ProtocolContext,
    marker: str,
) -> object:
    assert marker == MARKER
    assert isinstance(provider, WrappedSecrets)
    assert provider.value == configuration["value"]
    return await provider.resolve("secret://fixture/absent/" + marker, context)


class Adapter:
    def __init__(
        self, mode: str = "normal", exercise: Callable[[], None] | None = None
    ) -> None:
        self.mode = mode
        self.exercise = exercise
        self.loop: asyncio.AbstractEventLoop | None = None
        self.configuration: JsonObject = {}
        self.exercises = SecretsConformanceInputs(
            failure,
            (REFERENCE, ""),
            "secret://fixture/missing",
            "secret://foreign/token",
            "fixture/",
            self.effect,
        )
        self.instance: object | None = None
        self.cleaned = False

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        assert await lifetime.observe(None) == ()
        self.loop = asyncio.get_running_loop()
        self.configuration = dict(config)
        value = config["value"]
        assert isinstance(value, str)
        self.exercises = SecretsConformanceInputs(
            failure,
            (REFERENCE, value),
            "secret://fixture/missing",
            "secret://foreign/token",
            "fixture/",
            self.effect,
        )
        config["value"] = "mutated-prepare-copy"
        fixture_data["value"] = "mutated-fixture-copy"
        if self.mode == "prepare":
            raise ValueError("private-preparation-marker")
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        assert asyncio.get_running_loop() is self.loop
        assert config == self.configuration
        if self.mode == "bind":
            raise ValueError("private-binding-marker")
        return ConstructorArguments((config["value"], self.mode))

    async def effect(
        self, provider: SecretsProvider, config: JsonObject, context: ProtocolContext
    ) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.instance = provider
        if self.exercise is not None:
            self.exercise()
        if self.mode == "exercise":
            raise ValueError("private-exercise-marker")
        assert config == self.configuration
        value = await provider.resolve(REFERENCE, context)
        assert value.reveal() == config["value"]

    async def cleanup(self, instance: object | None) -> None:
        assert asyncio.get_running_loop() is self.loop
        assert not self.cleaned
        self.cleaned = True
        if instance is not None:
            assert isinstance(instance, WrappedSecrets)
            if self.instance is not None:
                assert instance is self.instance
            await instance.close()
        if self.mode == "cleanup":
            raise ValueError("private-cleanup-marker")
