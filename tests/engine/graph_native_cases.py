from typing import cast

from kora.ir.json import JsonObject, JsonValue
from tests.engine.graph_output_cases import child, output_arguments, output_document


def native_document(name: str) -> JsonObject:
    raw = output_document("command" if name == "CommandResult" else "agent")
    if name == "VcsRef":
        child(raw, "type_contracts")["VcsRef"] = {"builtin_revision": "1"}
        child(raw, "graph", "nodes", "producer", "outputs")["publication"] = {
            "type": "VcsRef"
        }
    return raw


def native_arguments(
    name: str, nested: bool = False, raw: JsonObject | None = None
) -> tuple[str, list[JsonValue], list[str]]:
    document = native_document(name) if raw is None else raw
    text, types, path = output_arguments(document, nested)
    path[-1] = {
        "AgentSession": "session",
        "CommandResult": "result",
        "VcsRef": "publication",
    }[name]
    return text, types, path


def native_record(value: JsonObject) -> JsonObject:
    records = cast(list[JsonObject], value["type_contracts"])
    return records[0]
