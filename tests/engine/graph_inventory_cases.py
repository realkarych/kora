from typing import cast

from kora.db.graph_value_json import TextBudget, render_object
from kora.engine.graph_output_inventory import build_graph_output_inventory
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_type_references import array_text
from kora.engine.graph_types import build_graph_type_contracts
from kora.ir.json import JsonObject, JsonValue
from tests.engine.graph_native_cases import native_document
from tests.engine.graph_output_cases import LIMIT, child, output_document


def inventory_arguments(raw: JsonObject) -> tuple[str, list[JsonValue]]:
    text = render_object(raw, TextBudget(LIMIT))
    types = build_graph_type_contracts(text, max_encoded_bytes=LIMIT)
    return text, saved_array(types)


def complete_inventory(arguments: tuple[str, list[JsonValue]]) -> str:
    result = build_graph_output_inventory(*arguments, max_encoded_bytes=LIMIT)
    assert isinstance(result, str)
    return result


def mixed_document() -> JsonObject:
    raw = native_document("VcsRef")
    command = output_document("command")
    human = output_document("human_input")
    wait = output_document("wait_event")
    child(raw, "type_contracts").update(child(command, "type_contracts"))
    child(raw, "graph", "nodes")["command"] = child(
        command, "graph", "nodes", "producer"
    )
    child(raw, "graph")["subgraphs"] = {
        "unused": {
            "nodes": {
                "wait": child(wait, "graph", "nodes", "producer"),
                "approve": child(human, "graph", "nodes", "producer"),
                "timer": {"kind": "timer", "config": {"duration": "0s"}, "outputs": {}},
            }
        }
    }
    return raw


def entries(text: str) -> list[JsonObject]:
    return cast(list[JsonObject], saved_array(text))


def canonical_size(value: object) -> int:
    return len(array_text(value, TextBudget(LIMIT)))
