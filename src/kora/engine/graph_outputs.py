import hashlib
from dataclasses import dataclass
from typing import cast

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_output_sources import (
    GraphOutputSource,
    graph_output_source,
    output_path,
)
from kora.engine.graph_schema_preparation import prepare_graph_schema
from kora.engine.graph_type_references import array_text, root_reference_text
from kora.engine.graph_types import read_document, read_graph_type_contracts
from kora.ir.json import JsonObject, JsonValue
from kora.ir.output_schemas import validate_output_schemas
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, POLICY
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal


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


def declared_chain(name: str, records: dict[str, JsonObject]) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    while name in records and name not in seen:
        seen.add(name)
        chain.append(name)
        record = records[name]
        parent = record["parent"]
        if parent is None:
            if name != "StructuredOutput" or record["builtin_revision"] != "1":
                break
            chain.reverse()
            return chain
        name = cast(str, parent)
    raise GraphValueRejected(GraphValueFailure.INVALID)


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


def role(name: str | None) -> JsonObject:
    return (
        {"kind": "inline_schema"}
        if name is None
        else {"kind": "type_schema", "name": name}
    )


def constraint_text(chain: list[str], inline: bool, budget: TextBudget) -> str:
    budget.reserve(2)
    texts: list[str] = []
    for name in chain:
        budget.reserve(int(bool(texts)))
        texts.append(render_object(role(name), budget))
    if inline:
        budget.reserve(int(bool(texts)))
        texts.append(render_object(role(None), budget))
    return "[" + ",".join(texts) + "]"


def effective_text(
    source: GraphOutputSource, records: list[JsonObject], budget: TextBudget
) -> str:
    by_name = {cast(str, record["name"]): record for record in records}
    chain = declared_chain(source.declared_type, by_name)
    references: list[JsonValue] = []
    if source.inline is not None:
        roots = {
            name: cast(JsonObject, record["schema"]) for name, record in by_name.items()
        }
        uris = {"urn:kora:artifact:" + name.replace("/", ":"): name for name in roots}
        references = saved_array(
            root_reference_text(
                source.inline, None, roots, uris, TextBudget(budget.remaining)
            )
        )
    selected = general_closure(chain, references, by_name)
    header = render_object(
        {
            "execution_semantics": "kora.execution.v1",
            "declared_type": source.declared_type,
            "checker_binding": "kora.structured-output-view.v1",
        },
        budget,
    )
    budget.reserve(len(',"type_contracts":'))
    types = array_text(selected, budget)
    budget.reserve(len(',"inline_schema":'))
    if source.inline is None:
        budget.reserve(4)
        inline = "null"
    else:
        inline = render_object(
            {
                "origin": source.origin,
                "source": source.source,
                "schema": source.inline,
                "references": references,
            },
            budget,
        )
    budget.reserve(len(',"checker":'))
    checker = render_object(
        role(
            None
            if source.declared_type == "StructuredOutput" and source.inline is not None
            else source.declared_type
        ),
        budget,
    )
    budget.reserve(len(',"constraints":'))
    constraints = constraint_text(chain, source.inline is not None, budget)
    budget.reserve(len(',"schema_validation":'))
    policy = render_object({"policy": POLICY, "parameters": dict(PARAMETERS)}, budget)
    return (
        header[:-1]
        + ',"type_contracts":'
        + types
        + ',"inline_schema":'
        + inline
        + ',"checker":'
        + checker
        + ',"constraints":'
        + constraints
        + ',"schema_validation":'
        + policy
        + "}"
    )


def derive_output(inputs: OutputInputs) -> tuple[str, list[JsonObject]]:
    document = read_document(inputs.ir_text, TextBudget(len(inputs.ir_text)))
    records = cast(list[JsonObject], saved_array(inputs.types_text))
    read_graph_type_contracts(
        inputs.ir_text,
        records,
        max_encoded_bytes=len(inputs.ir_text) + len(inputs.types_text),
    )
    if not validate_output_schemas(document).output_schemas_valid:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    selected = output_path(saved_array(inputs.path_text))
    roots = {
        cast(str, record["name"]): cast(JsonObject, record["schema"])
        for record in records
    }
    source = graph_output_source(ObjectReader(inputs.ir_text).object(), selected, roots)
    return effective_text(source, records, TextBudget(inputs.remaining)), records


def prepared_output(
    ir_text: str, records: list[JsonObject], text: str, max_encoded_bytes: int
) -> GraphOutputContract | StructuredSchemaPreparationRefusal:
    saved = ObjectReader(text).object()
    inline = saved["inline_schema"]
    candidate: JsonObject = {
        "declared_type": saved["declared_type"],
        "inline_schema": None
        if inline is None
        else {
            "source": cast(JsonObject, inline)["source"],
            "schema": cast(JsonObject, inline)["schema"],
        },
        "execution_semantics": saved["execution_semantics"],
        "checker_binding": saved["checker_binding"],
        "schema_validation": saved["schema_validation"],
    }
    prepared = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=max_encoded_bytes
    )
    if isinstance(prepared, StructuredSchemaPreparationRefusal):
        return prepared
    return GraphOutputContract(text, hashlib.sha256(text.encode("ascii")).hexdigest())


def build_graph_output_contract(
    ir_text: str, type_contracts: object, path: object, *, max_encoded_bytes: int
) -> GraphOutputContract | StructuredSchemaPreparationRefusal:
    inputs = admit_output_inputs(
        ir_text, type_contracts, path, TextBudget(max_encoded_bytes)
    )
    text, records = derive_output(inputs)
    return prepared_output(ir_text, records, text, max_encoded_bytes)


def read_graph_output_contract(
    ir_text: str,
    type_contracts: object,
    path: object,
    value: object,
    digest: str,
    *,
    max_encoded_bytes: int,
) -> GraphOutputContract | StructuredSchemaPreparationRefusal:
    budget = TextBudget(max_encoded_bytes)
    inputs = admit_output_inputs(ir_text, type_contracts, path, budget)
    text = render_object(value, budget)
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if hashlib.sha256(text.encode("ascii")).hexdigest() != digest:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    expected, records = derive_output(inputs)
    if text != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return prepared_output(ir_text, records, text, max_encoded_bytes)
