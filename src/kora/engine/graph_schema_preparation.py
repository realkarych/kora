from collections.abc import Iterator
from typing import cast

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_type_references import array_text
from kora.engine.graph_types import invalid_schema, read_graph_type_contracts
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import Location, SavedReferences, schema_sites
from kora.ir.schema_validation import validate_schema
from kora.ir.structured_schema_evaluation_policy import bind_evaluation_policy
from kora.ir.structured_schema_input import (
    PreparedStructuredSchemaInput,
    StructuredSchemaPreparationInput,
    StructuredSchemaPreparationRefusal,
)
from kora.ir.structured_schema_preparation import prepare_structured_schema_input

CANDIDATE_FIELDS: frozenset[str] = frozenset(
    {
        "declared_type",
        "inline_schema",
        "execution_semantics",
        "checker_binding",
        "schema_validation",
    }
)
EXECUTABLE_REFERENCES: frozenset[str] = frozenset({"$ref", "$dynamicRef"})


def admit_inputs(
    ir_text: str, type_contracts: object, candidate: object, max_encoded_bytes: int
) -> tuple[str, str]:
    budget = TextBudget(max_encoded_bytes)
    if type(ir_text) is not str or type(type_contracts) is not list:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(ir_text))
    if not ir_text.isascii() or type(candidate) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    types_text = array_text(cast(list[object], type_contracts), budget)
    candidate_text = render_object(cast(dict[object, object], candidate), budget)
    return types_text, candidate_text


def checked_inline(
    value: JsonValue, roots: dict[str, JsonObject]
) -> JsonObject | bool | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"source", "schema"}:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    schema = value["schema"]
    if type(schema) is bool:
        return schema
    if not isinstance(schema, dict):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    validate_schema(
        schema,
        Location(("inline_schema",)),
        SavedReferences(roots),
        KeywordValidation(invalid_schema),
        invalid_schema,
    )
    return schema


def inline_targets(schema: JsonObject, uris: dict[str, str]) -> Iterator[str]:
    for position, _ in schema_sites(schema, Location(())):
        if not isinstance(position, dict):
            continue
        for keyword in EXECUTABLE_REFERENCES:
            reference = position.get(keyword)
            if isinstance(reference, str):
                base, _, _ = reference.partition("#")
                if base:
                    yield uris[base]


def record_targets(record: JsonObject) -> Iterator[str]:
    parent = record["parent"]
    if isinstance(parent, str):
        yield parent
    for value in cast(list[JsonValue], record["references"]):
        reference = cast(JsonObject, value)
        path = cast(list[JsonValue], reference["path"])
        if path[-1] in EXECUTABLE_REFERENCES:
            yield cast(str, reference["target_type"])


def select_records(
    records: list[JsonObject], declared: JsonValue, inline: JsonObject | bool | None
) -> list[JsonObject]:
    by_name = {cast(str, record["name"]): record for record in records}
    if type(declared) is not str or declared not in by_name:
        return []
    selected: set[str] = {declared}
    queue = [declared]
    if isinstance(inline, dict):
        uris = {"urn:kora:artifact:" + name.replace("/", ":"): name for name in by_name}
        for name in inline_targets(inline, uris):
            if name not in selected:
                selected.add(name)
                queue.append(name)
    cursor = 0
    while cursor < len(queue):
        for name in record_targets(by_name[queue[cursor]]):
            if name not in selected:
                selected.add(name)
                queue.append(name)
        cursor += 1
    return [
        {key: value for key, value in by_name[name].items() if key != "references"}
        for name in sorted(selected)
    ]


def prepare_graph_schema(
    ir_text: str,
    type_contracts: object,
    candidate: object,
    *,
    max_encoded_bytes: int,
) -> PreparedStructuredSchemaInput | StructuredSchemaPreparationRefusal:
    types_text, candidate_text = admit_inputs(
        ir_text, type_contracts, candidate, max_encoded_bytes
    )
    saved = ObjectReader('{"v":' + types_text + "}").object()["v"]
    owned = ObjectReader(candidate_text).object()
    if owned.keys() != CANDIDATE_FIELDS:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    read_graph_type_contracts(
        ir_text, saved, max_encoded_bytes=len(ir_text) + len(types_text)
    )
    policy = bind_evaluation_policy(owned["schema_validation"])
    records = cast(list[JsonObject], saved)
    roots = {
        cast(str, record["name"]): cast(JsonObject, record["schema"])
        for record in records
    }
    inline = checked_inline(owned["inline_schema"], roots)
    selected = select_records(records, owned["declared_type"], inline)
    return prepare_structured_schema_input(
        StructuredSchemaPreparationInput(
            declared_type=cast(str, owned["declared_type"]),
            type_contracts=selected,
            inline_schema=owned["inline_schema"],
            execution_semantics=cast(str, owned["execution_semantics"]),
            checker_binding=cast(str, owned["checker_binding"]),
            preparation_parameters=policy.preparation_parameters(),
        )
    )
