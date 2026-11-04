from collections.abc import Iterator
from typing import cast

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
)
from kora.engine.graph_native_outputs import build_graph_native_output_contract
from kora.engine.graph_native_sources import VIEW_BYTES
from kora.engine.graph_output_sources import object_at
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_outputs import build_graph_output_contract
from kora.engine.graph_type_references import array_text
from kora.engine.graph_types import read_document, read_graph_type_contracts
from kora.ir.json import JsonObject
from kora.ir.output_schemas import validate_output_schemas
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal


def capture_inventory_types(
    ir_text: str, type_contracts: object, budget: TextBudget
) -> str:
    if type(ir_text) is not str or type(type_contracts) is not list:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(ir_text))
    if not ir_text.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return array_text(cast(list[object], type_contracts), budget)


def node_output_paths(body: JsonObject, prefix: list[str]) -> Iterator[list[str]]:
    for name, value in object_at(body, ["nodes"]).items():
        node = cast(JsonObject, value)
        for output in cast(JsonObject, node.get("outputs", {})):
            yield [*prefix, "nodes", name, "outputs", output]


def inventory_paths(raw: JsonObject) -> Iterator[list[str]]:
    graph = object_at(raw, ["graph"])
    yield from node_output_paths(graph, ["graph"])
    for name, value in cast(JsonObject, graph.get("subgraphs", {})).items():
        body = cast(JsonObject, value)
        prefix = ["graph", "subgraphs", name]
        yield from node_output_paths(body, prefix)
        for output in cast(JsonObject, body.get("outputs", {})):
            yield [*prefix, "outputs", output]


def structured_ancestry(name: str, records: dict[str, JsonObject]) -> bool:
    while name in records:
        parent = records[name]["parent"]
        if parent is None:
            return name == "StructuredOutput"
        name = cast(str, parent)
    return False


def derive_inventory(
    ir_text: str, types_text: str, budget: TextBudget
) -> str | StructuredSchemaPreparationRefusal:
    document = read_document(ir_text, TextBudget(len(ir_text)))
    records = cast(list[JsonObject], saved_array(types_text))
    source_size = len(ir_text) + len(types_text)
    read_graph_type_contracts(ir_text, records, max_encoded_bytes=source_size)
    if not validate_output_schemas(document).output_schemas_valid:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    raw = ObjectReader(ir_text).object()
    by_name = {cast(str, record["name"]): record for record in records}
    paths: list[str] = []
    for path in inventory_paths(raw):
        paths.append(array_text(path, budget))
    paths.sort()
    budget.reserve(2)
    entries: list[str] = []
    for path_text in paths:
        budget.reserve(
            len('{"path":,"contract":,"contract_digest":""}')
            + 64
            + len(path_text)
            + int(bool(entries))
        )
        prepaid = source_size + len(path_text)
        budget.reserve(prepaid)
        selected = cast(list[str], saved_array(path_text))
        if len(selected) == 5 and selected[1] == "subgraphs":
            raise GraphValueRejected(GraphValueFailure.VERSION)
        name = cast(str, object_at(raw, selected)["type"])
        if name == "AgentSession":
            budget.reserve(VIEW_BYTES)
            prepaid += VIEW_BYTES
        if name in {"AgentSession", "CommandResult", "VcsRef"}:
            result = build_graph_native_output_contract(
                ir_text,
                records,
                selected,
                max_encoded_bytes=prepaid + budget.remaining,
            )
        elif structured_ancestry(name, by_name):
            result = build_graph_output_contract(
                ir_text,
                records,
                selected,
                max_encoded_bytes=prepaid + budget.remaining,
            )
        else:
            raise GraphValueRejected(GraphValueFailure.VERSION)
        if isinstance(result, StructuredSchemaPreparationRefusal):
            return result
        budget.reserve(len(result.text))
        entries.append(
            '{"path":'
            + path_text
            + ',"contract":'
            + result.text
            + ',"contract_digest":"'
            + result.digest
            + '"}'
        )
    return "[" + ",".join(entries) + "]"


def build_graph_output_inventory(
    ir_text: str, type_contracts: object, *, max_encoded_bytes: int
) -> str | StructuredSchemaPreparationRefusal:
    budget = TextBudget(max_encoded_bytes)
    types_text = capture_inventory_types(ir_text, type_contracts, budget)
    return derive_inventory(ir_text, types_text, budget)


def read_graph_output_inventory(
    ir_text: str, type_contracts: object, value: object, *, max_encoded_bytes: int
) -> str | StructuredSchemaPreparationRefusal:
    budget = TextBudget(max_encoded_bytes)
    types_text = capture_inventory_types(ir_text, type_contracts, budget)
    if type(value) is not list:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    text = array_text(cast(list[object], value), budget)
    expected = derive_inventory(ir_text, types_text, budget)
    if isinstance(expected, StructuredSchemaPreparationRefusal):
        return expected
    if text != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return text
