import atexit
import json
import os
import resource
import socket
import sys
import tracemalloc
from pathlib import Path

from kora.ir.json import JsonObject
from kora.protocols.metadata import ProviderMetadata
from kora_tooling_fixture.control import configuration
from kora_tooling_fixture.providers import metadata

if not tracemalloc.is_tracing():
    tracemalloc.start(1)


def measured_metadata(name: str) -> ProviderMetadata:
    schema: JsonObject = {"type": "object", "description": ""}
    overhead = len(json.dumps(schema, separators=(",", ":")).encode())
    schema["description"] = "x" * (8192 - overhead)
    return metadata(name, schema)


def counters(phase: str) -> bytes:
    current, peak = tracemalloc.get_traced_memory()
    return (
        json.dumps(
            {
                "phase": phase,
                "pid": os.getpid(),
                "heap_bytes": current,
                "heap_peak_bytes": peak,
                "native_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1024 if sys.platform == "linux" else 1),
                "imports": len(sys.modules),
                "root": str(Path.cwd().parent),
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def final_counters(endpoint: socket.socket) -> None:
    try:
        endpoint.sendall(counters("final"))
    finally:
        endpoint.close()


def controlled() -> ProviderMetadata:
    result = measured_metadata("zz_controlled")
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.connect(str(Path(configuration().socket) / f"{os.getppid()}.sock"))
    atexit.register(final_counters, endpoint)
    endpoint.sendall(counters("barrier"))
    action = endpoint.recv(1)
    if action == b"f":
        raise ValueError("declared metadata failure")
    if action != b"r":
        raise ValueError("unexpected measurement action")
    return result
