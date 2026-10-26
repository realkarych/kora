import os

import typer

from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.providers import RAW_MARKER, good

typer.echo(RAW_MARKER)
os.write(2, RAW_MARKER.encode())


def provider_metadata() -> ProviderMetadata:
    return good()
