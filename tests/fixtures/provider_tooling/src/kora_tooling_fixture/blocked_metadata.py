import os
import socket
from pathlib import Path

from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.configured_secrets import configured
from kora_tooling_fixture.control import configuration


def blocked() -> ProviderMetadata:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.connect(str(Path(configuration().socket) / f"{os.getppid()}.sock"))
        endpoint.sendall(b"bootstrap")
        if endpoint.recv(1) != b"a":
            raise ValueError("bootstrap barrier refused")
    return configured()
