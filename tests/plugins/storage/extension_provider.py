import json
from collections.abc import Callable
from importlib.resources import files

from kora.ir.json import JsonObject, parse_json_object
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage


def configure(
    configuration: JsonObject, constructor: Callable[[JsonObject], ArtifactStorage]
) -> ArtifactStorage:
    return constructor(configuration)


def provider_metadata() -> ProviderMetadata:
    document: object = json.loads(
        files("kora_storage_extension").joinpath("schema.json").read_text()
    )
    return ProviderMetadata(
        ProtocolName.ARTIFACT_STORAGE,
        "fixture_local",
        configure,
        parse_json_object(document),
        "1",
    )
