import os
import warnings

import typer

from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.providers import RAW_MARKER, good

typer.echo(RAW_MARKER)
os.write(2, RAW_MARKER.encode())
warnings.warn(RAW_MARKER, UserWarning, stacklevel=1)


def provider_metadata() -> ProviderMetadata:
    return good()
