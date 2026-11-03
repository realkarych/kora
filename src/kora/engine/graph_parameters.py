from typing import cast

from pydantic import MISSING

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.types import JsonObject, JsonValue
from kora.ir.data import IrJsonObjectField
from kora.ir.document import GraphDocument

PARAMETER_INTERPRETATION = "kora.graph-parameters.v1"
PARAMETER_PREFIX = '{"interpretation":"kora.graph-parameters.v1","schema":'


def parameter_schema_text(document: GraphDocument, budget: TextBudget) -> str:
    budget.reserve(len(PARAMETER_PREFIX) + 1)
    params = document.graph.params
    if params is MISSING:
        schema: JsonObject = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    else:
        if type(params) is not IrJsonObjectField:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        count = dict[str, JsonValue].__len__(params)
        minimum_size = 5 * count + 1 if count else 2
        if minimum_size > budget.remaining:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        schema = dict[str, JsonValue].copy(params)
    return render_object(schema, budget, reject_aliases=True)


def build_graph_parameter_binding(
    document: GraphDocument, *, max_encoded_bytes: int
) -> str:
    schema = parameter_schema_text(document, TextBudget(max_encoded_bytes))
    return PARAMETER_PREFIX + schema + "}"


def read_graph_parameter_binding(
    document: GraphDocument, value: object, *, max_encoded_bytes: int
) -> str:
    budget = TextBudget(max_encoded_bytes)
    if type(value) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    mapping = cast(dict[object, object], value)
    if len(mapping) != 2:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    text = render_object(mapping, budget, reject_aliases=True)
    saved = ObjectReader(text).object()
    if set(saved) != {"interpretation", "schema"}:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if saved["interpretation"] != PARAMETER_INTERPRETATION:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    schema = render_object(saved["schema"], TextBudget(max_encoded_bytes))
    expected = parameter_schema_text(document, TextBudget(max_encoded_bytes))
    if schema != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return text
