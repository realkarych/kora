from collections.abc import Callable

import pytest

from kora.conformance.capture import Capture
from kora.ir.json import JsonObject
from kora.plugins.storage.local import LocalArtifactStorage, create_provider
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage


def construct(metadata: ProviderMetadata, configuration: JsonObject) -> ArtifactStorage:
    created: list[LocalArtifactStorage] = []

    def constructor(supplied: JsonObject) -> ArtifactStorage:
        assert supplied == configuration
        instance = create_provider(supplied)
        created.append(instance)
        return instance

    instance = metadata.implementation(configuration, constructor)
    assert len(created) == 1
    assert instance is created[0]
    assert metadata.config_schema == created[0].config_schema
    return created[0]


@pytest.fixture
def storage_extension_factory(
    kora_capture: Capture,
) -> Callable[[JsonObject], ArtifactStorage]:
    metadata = load_provider(
        ProtocolName.ARTIFACT_STORAGE, "fixture_local", kora_capture.diagnostics
    )
    return lambda configuration: construct(metadata, configuration)
