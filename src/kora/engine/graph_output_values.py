from dataclasses import dataclass
from typing import cast

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
)
from kora.engine.graph_type_references import array_text
from kora.ir.json import JsonObject, JsonValue


@dataclass(frozen=True, slots=True, repr=False)
class GraphOutputContract:
    text: str
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class OutputInputs:
    ir_text: str
    types_text: str
    path_text: str
    remaining: int


def admit_output_inputs(
    ir_text: str, type_contracts: object, path: object, budget: TextBudget
) -> OutputInputs:
    if (
        type(ir_text) is not str
        or type(type_contracts) is not list
        or type(path) is not list
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(ir_text))
    if not ir_text.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    types_text = array_text(cast(list[object], type_contracts), budget)
    path_text = array_text(cast(list[object], path), budget)
    return OutputInputs(ir_text, types_text, path_text, budget.remaining)


def saved_array(text: str) -> list[JsonValue]:
    value = ObjectReader('{"v":' + text + "}").object()["v"]
    if not isinstance(value, list):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return value


def general_closure(
    chain: list[str], references: list[JsonValue], records: dict[str, JsonObject]
) -> list[JsonObject]:
    selected = set(chain)
    queue = list(chain)
    for reference in references:
        name = cast(JsonObject, reference)["target_type"]
        if isinstance(name, str) and name not in selected:
            selected.add(name)
            queue.append(name)
    cursor = 0
    while cursor < len(queue):
        record = records[queue[cursor]]
        parent = record["parent"]
        if isinstance(parent, str) and parent not in selected:
            selected.add(parent)
            queue.append(parent)
        for reference in cast(list[JsonValue], record["references"]):
            name = cast(str, cast(JsonObject, reference)["target_type"])
            if name not in selected:
                selected.add(name)
                queue.append(name)
        cursor += 1
    return [records[name] for name in sorted(selected)]
