import base64
import json
from hashlib import sha256
from pathlib import Path
from typing import cast

from kora.ir import CommandResult
from kora.ir.json import JsonObject, JsonValue
from tests.ir.test_command_result_contract import ARTIFACTS


def vectors() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "CommandResult.vectors.json").read_bytes()),
    )


def fixture(name: str = "empty-success") -> JsonObject:
    return cast("JsonObject", json.loads(fixture_bytes(name)))


def fixture_bytes(name: str = "empty-success") -> bytes:
    return (ARTIFACTS / f"examples/command-result/valid/{name}.json").read_bytes()


def rows(category: str) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", vectors()[category])


def stream_bytes(
    payload: bytes, observed: int | None = None, eof: bool = True
) -> JsonObject:
    size = len(payload)
    count = size if observed is None else observed
    return {
        "storage_ref": "capture-ref",
        "payload_digest": "sha256:" + sha256(payload).hexdigest(),
        "size": size,
        "observed_bytes": count,
        "eof": eof,
        "truncated": count > size or not eof,
        "tail_base64": base64.b64encode(payload[-4096:]).decode(),
    }


def with_stream(payload: bytes) -> JsonObject:
    value = fixture()
    value["stdout"] = stream_bytes(payload)
    return value


def resource_path(size: int) -> str:
    count = (size + 256) // 256
    width, remainder = divmod(size - count + 1, count)
    return "/".join("a" * (width + int(index < remainder)) for index in range(count))


def with_resources(names: list[str], size: int = 0) -> JsonObject:
    value = fixture()
    resources: list[JsonValue] = [
        {
            "name": name,
            "sha256": "sha256:" + "0" * 64,
            "size": size,
            "executable": False,
        }
        for name in sorted(names, key=lambda name: name.encode())
    ]
    value["resources"] = resources
    value["resource_set_digest"] = (
        "sha256:"
        + sha256(
            json.dumps(
                resources, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if resources
        else None
    )
    return value


def with_paths(paths: list[str]) -> JsonObject:
    value = fixture()
    value["protected_paths"] = [cast("JsonValue", path) for path in paths]
    value["protection"] = "verified" if paths else "unprotected"
    return value


def dense() -> JsonObject:
    value = with_resources(
        [f"{index:02d}/" + resource_path(4093) for index in range(64)], 262144
    )
    value["protected_paths"] = [cast("JsonValue", '"' * 4096) for _ in range(16)]
    value["protection"] = "verified"
    value["repo"] = '"' * 4096
    value["commit"] = '"' * 255
    value["base_commit"] = '"' * 255
    value["stdout"] = stream_bytes(b"a" * 4097)
    value["stderr"] = stream_bytes(b"b" * 4097)
    return value


def forged(raw: object) -> CommandResult:
    value = object.__new__(CommandResult)
    object.__setattr__(value, "_canonical", raw)
    return value


def structural_invalid_paths() -> list[Path]:
    return sorted((ARTIFACTS / "examples/command-result/invalid").glob("*.json"))
