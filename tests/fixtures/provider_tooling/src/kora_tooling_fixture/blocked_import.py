from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.control import controlled

METADATA: ProviderMetadata = controlled()


def provider_metadata() -> ProviderMetadata:
    return METADATA
