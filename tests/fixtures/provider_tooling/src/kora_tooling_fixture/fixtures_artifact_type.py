import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.artifact_type import ArtifactType
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora_tooling_fixture.configured_artifact_type import (
    VIOLATIONS,
    ConfiguredArtifactType,
)


def payload() -> JsonValue:
    return {"left": "private-left-marker", "right": "private-right-marker"}


def equivalent() -> JsonValue:
    return {"right": "private-right-marker", "left": "private-left-marker"}


def rejected(marker: str) -> JsonValue:
    return {"left": marker, "right": marker}


async def failure(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    return provider.summary(rejected(marker), context)


async def effect(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext
) -> None:
    selected = config["selected"]
    assert selected in {"left", "right"}
    expected = "private-left-marker" if selected == "left" else "private-right-marker"
    assert provider.summary(payload(), context) == expected
    assert provider.digest_fields(payload(), context) == ["/" + str(selected)]


class Adapter:
    exercises: ArtifactTypeConformanceInputs
    ignore_configuration: bool

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        selected = config["selected"]
        assert isinstance(selected, str)
        self.ignore_configuration = fixture_data.get("ignore_configuration") is True
        expected = (
            "private-left-marker" if selected == "left" else "private-right-marker"
        )
        self.exercises = ArtifactTypeConformanceInputs(
            (
                ArtifactTypeConformanceCase(
                    payload, equivalent, ("/" + selected,), lambda: expected
                ),
            ),
            rejected,
            VIOLATIONS,
            failure,
            effect,
        )
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments(
            args=({"selected": "left"} if self.ignore_configuration else config,)
        )

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or isinstance(instance, ConfiguredArtifactType)


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
