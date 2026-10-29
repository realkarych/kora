import json
from copy import deepcopy
from hashlib import sha256
from typing import cast

from kora.ir import AgentSession
from kora.ir.json import JsonObject, JsonValue
from tests.ir.test_agent_session_contract import ARTIFACTS


def rows(category: str) -> list[dict[str, object]]:
    vectors = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.vectors.json").read_bytes()),
    )
    return cast("list[dict[str, object]]", vectors[category])


def fixture_bytes(name: str = "captured-empty") -> bytes:
    return (ARTIFACTS / f"examples/agent-session/{name}.json").read_bytes()


def fixture(name: str = "captured-empty") -> JsonObject:
    return cast("JsonObject", json.loads(fixture_bytes(name)))


def metadata(value: JsonObject) -> JsonObject:
    return cast("JsonObject", value["metadata"])


def first_usage(value: JsonObject) -> JsonObject:
    return cast("list[JsonObject]", metadata(value)["usage"])[0]


def child(value: object, part: str | int) -> object:
    if isinstance(part, int):
        return cast("list[object]", value)[part]
    return cast("dict[str, object]", value)[part]


def changed(path: tuple[str | int, ...], replacement: object) -> dict[str, object]:
    result = cast("dict[str, object]", fixture())
    current: object = result
    for part in path[:-1]:
        current = child(current, part)
    final = path[-1]
    if isinstance(final, int):
        cast("list[object]", current)[final] = replacement
    else:
        cast("dict[str, object]", current)[final] = replacement
    return result


def dense() -> JsonObject:
    value = fixture("denial-sample-incomplete")
    meta = metadata(value)
    row = first_usage(value)
    row["observation_id"] = "🦉" * 256
    row["input_tokens"] = 2**31 - 1
    row["output_tokens"] = 2**31 - 1
    row["reasoning_tokens"] = 2**31 - 1
    row["usd_estimate"] = "99999999999999.9999999999"
    meta["usage"] = [
        {**deepcopy(row), "model": f"{index:02d}" + "🦉" * 254} for index in range(64)
    ]
    meta["duration_ms"] = 2**63 - 1
    meta["turns"] = 2**31 - 1
    meta["price_table_id"] = "🦉" * 256
    denial: JsonObject = {
        "tool": "\\" * 256,
        "tool_use_id": '"' * 256,
        "source": "policy",
        "pattern": "namespace.deny.9999",
        "code": "denied",
    }
    value["denials"] = {
        "items": [deepcopy(denial) for _ in range(1000)],
        "total": 2**63 - 1,
        "complete": True,
    }
    return value


def with_transcript(payload: bytes, *, truncated: bool = False) -> JsonObject:
    value = fixture()
    digest = sha256(payload).hexdigest()
    value["transcript"] = {
        "capture": "captured",
        "source_size": 268435457 if truncated else len(payload),
        "source_sha256": "0" * 64 if truncated else digest,
        "source_verified": not truncated,
        "retained_size": len(payload),
        "retained_sha256": digest,
        "truncated": truncated,
    }
    return value


def forged(raw: object) -> AgentSession:
    value = object.__new__(AgentSession)
    object.__setattr__(value, "_canonical", raw)
    return value


def aliased() -> JsonObject:
    value = fixture("denial-sample-incomplete")
    denial = cast("JsonObject", value["denials"])
    item = cast("list[JsonValue]", denial["items"])[0]
    denial["items"] = [item, item]
    return value
