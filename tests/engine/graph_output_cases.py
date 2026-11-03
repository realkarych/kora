from typing import cast

from kora.db.graph_value_json import ObjectReader, TextBudget, render_object
from kora.engine.graph_outputs import GraphOutputContract, build_graph_output_contract
from kora.engine.graph_type_references import array_text
from kora.engine.graph_types import build_graph_type_contracts
from kora.ir.json import JsonObject, JsonValue

LIMIT = 1_000_000


def output_document(kind: str = "agent") -> JsonObject:
    config: JsonObject
    outputs: JsonObject
    contracts: JsonObject = {"StructuredOutput": {"builtin_revision": "1"}}
    if kind == "agent":
        config = {"harness": "claude", "prompt": "", "workspace": {"repo": ""}}
        outputs = {
            "session": {"type": "AgentSession"},
            "value": {"type": "StructuredOutput"},
        }
        contracts["AgentSession"] = {"builtin_revision": "1"}
    elif kind == "command":
        config = {
            "cmd": ["true"],
            "workspace": {"repo": ""},
            "output_file": "output.json",
        }
        outputs = {
            "result": {"type": "CommandResult"},
            "value": {"type": "StructuredOutput"},
        }
        contracts["CommandResult"] = {"builtin_revision": "1"}
    elif kind == "human_input":
        config = {"channel": "cli", "timeout": "1h", "on_expiry": "fail"}
        outputs = {"response": {"type": "StructuredOutput"}}
    else:
        config = {
            "event": {"type": "webhook", "name": "ready"},
            "timeout": "1h",
            "on_expiry": "skip",
        }
        outputs = {"event": {"type": "StructuredOutput"}}
    return {
        "ir_version": "1.0",
        "type_contracts": contracts,
        "graph": {
            "id": "graph",
            "namespace": "example",
            "nodes": {
                "producer": {
                    "kind": kind,
                    "source": {
                        "file": "private-source.py",
                        "line": 7,
                        "x-source": {"z": "\0", "a": -0.0},
                    },
                    "config": config,
                    "outputs": outputs,
                }
            },
        },
        "runtime": {"runtime": "local", "vcs": "git"},
    }


def child(root: JsonObject, *path: str) -> JsonObject:
    value = root
    for name in path:
        value = cast(JsonObject, value[name])
    return value


def output_arguments(
    raw: JsonObject, nested: bool = False
) -> tuple[str, list[JsonValue], list[str]]:
    node = child(raw, "graph", "nodes", "producer")
    name = {"human_input": "response", "wait_event": "event"}.get(
        cast(str, node["kind"]), "value"
    )
    path = ["graph", "nodes", "producer", "outputs", name]
    if nested:
        graph = child(raw, "graph")
        graph["subgraphs"] = {"body": {"nodes": graph["nodes"]}}
        graph["nodes"] = {"timer": {"kind": "timer", "config": {"duration": "0s"}}}
        path = ["graph", "subgraphs", "body", "nodes", "producer", "outputs", name]
    text = render_object(raw, TextBudget(LIMIT))
    section = build_graph_type_contracts(text, max_encoded_bytes=LIMIT)
    values = ObjectReader('{"v":' + section + "}").object()["v"]
    assert isinstance(values, list)
    return text, values, path


def built(
    arguments: tuple[str, list[JsonValue], list[str]], limit: int = LIMIT
) -> GraphOutputContract:
    result = build_graph_output_contract(*arguments, max_encoded_bytes=limit)
    assert isinstance(result, GraphOutputContract)
    return result


def input_bytes(arguments: tuple[str, list[JsonValue], list[str]]) -> int:
    ir, types, path = arguments
    return (
        len(ir)
        + len(array_text(types, TextBudget(LIMIT)))
        + len(array_text(path, TextBudget(LIMIT)))
    )
