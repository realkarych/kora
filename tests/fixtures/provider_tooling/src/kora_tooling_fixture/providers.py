import json
import os
import sys
from pathlib import Path

import typer

from kora.ir.json import JsonObject
from kora.protocols.metadata import ProtocolName, ProviderMetadata

RAW_MARKER: str = "private-provider-output-248"
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "pytest",
    "jsonschema",
    "kora.conformance",
    "kora.testing",
)
FORBIDDEN_ENVIRONMENT: frozenset[str] = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "KORA_TOKEN", "PYTEST_ADDOPTS", "OPENAI_API_KEY"}
)


def constructor() -> object:
    raise RuntimeError("listing must never construct a provider")


def metadata(name: str, schema: JsonObject | None = None) -> ProviderMetadata:
    if any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
        for prefix in FORBIDDEN_IMPORTS
    ):
        raise RuntimeError("listing imported an excluded module")
    if FORBIDDEN_ENVIRONMENT.intersection(os.environ):
        raise RuntimeError("listing inherited ambient configuration")
    if os.listdir("."):
        raise RuntimeError("listing did not start in an empty directory")
    return ProviderMetadata(
        protocol=ProtocolName.POLICY,
        name=name,
        implementation=constructor,
        config_schema={"type": "object"} if schema is None else schema,
        version="1.0.0",
    )


def good() -> ProviderMetadata:
    typer.echo(RAW_MARKER)
    os.write(1, RAW_MARKER.encode())
    os.write(2, RAW_MARKER.encode())
    return metadata("external")


def broken() -> ProviderMetadata:
    typer.echo(RAW_MARKER, err=True)
    raise RuntimeError(RAW_MARKER)


def saturated() -> ProviderMetadata:
    chunk = (RAW_MARKER * 2048).encode()
    for _ in range(80):
        os.write(1, chunk)
        os.write(2, chunk)
    return metadata("saturated")


def largest_schema() -> ProviderMetadata:
    schema: JsonObject = {"type": "object", "description": ""}
    overhead = len(json.dumps(schema, separators=(",", ":")).encode())
    schema["description"] = "x" * (1048576 - overhead)
    return metadata("largest", schema)


def retained_file() -> ProviderMetadata:
    result = metadata("retained")
    Path("provider-owned.txt").write_text(RAW_MARKER)
    return result


def changed_socket() -> ProviderMetadata:
    result = metadata("changed")
    Path("../r").chmod(0o666)
    return result
