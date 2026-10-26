import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.providers import metadata

CHILDREN: list[subprocess.Popen[bytes]] = []


class Control(BaseModel):
    socket: str
    resistant: bool = False
    retained: bool = False


def configuration() -> Control:
    return Control.model_validate_json(
        Path(__file__).with_name("control.json").read_bytes()
    )


def connect() -> socket.socket:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        endpoint.connect(configuration().socket)
        endpoint.sendall(json.dumps({"pid": os.getpid()}).encode() + b"\n")
        return endpoint
    except OSError:
        endpoint.close()
        raise


def controlled() -> ProviderMetadata:
    result = metadata("controlled")
    if configuration().retained:
        Path("provider-owned.txt").write_text("provider-owned-content")
    if configuration().resistant:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with connect() as endpoint:
        action = endpoint.recv(1)
        if action == b"x":
            raise SystemExit(23)
        if action == b"d":
            CHILDREN.append(
                subprocess.Popen(
                    [sys.executable, "-I", "-m", "kora_tooling_fixture.descendant"],
                    shell=False,
                )
            )
        elif action != b"r":
            raise ValueError("unknown fixture action")
    return result
