from collections.abc import Generator

import pytest

from kora.ir.json import JsonObject
from kora.protocols.lifetime import ConformanceLifetime


class WrongBundle:
    exercises: object = object()

    def __init__(self) -> None:
        self.cleaned = False

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> "WrongBundle":
        return self

    def bind_configuration(self, config: JsonObject) -> None:
        raise AssertionError("wrong bundle must fail before construction")

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None
        self.cleaned = True


@pytest.fixture
def kora_conformance_adapter() -> Generator[WrongBundle]:
    adapter = WrongBundle()

    yield adapter

    assert adapter.cleaned
