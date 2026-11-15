import json
from dataclasses import dataclass, field
from typing import cast

from kora.ir.json import JsonObject, JsonValue
from kora.ir.standard_schema_policy import (
    SchemaDecision,
    SchemaDomain,
    SchemaRefused,
    SourceAccounting,
    bind_policy,
    record,
    require,
)
from kora.ir.standard_schema_support import check_standard_schema_source
from kora.ir.standard_schema_validation import validate_standard_schema

PARAMETER_INTERPRETATION = "kora.graph-parameters.v1"


@dataclass(frozen=True, slots=True)
class StandardParameterInput:
    parameters: object
    documents: object
    parameter_evaluation: object


@dataclass(frozen=True, slots=True)
class PreparedStandardParameters:
    source_bytes: bytes = field(repr=False)
    accounting: SourceAccounting


def prepare_standard_parameters(
    value: StandardParameterInput,
) -> PreparedStandardParameters:
    require(type(value) is StandardParameterInput)
    limits = bind_policy(SchemaDomain.PARAMETERS, value.parameter_evaluation)
    require(type(value.parameters) is dict)
    parameters = cast("dict[str, object]", value.parameters)
    require(len(parameters) == 2 and set(parameters) == {"interpretation", "schema"})
    require(type(parameters["interpretation"]) is str)
    require(parameters["interpretation"] == PARAMETER_INTERPRETATION)
    require(type(parameters["schema"]) is dict)
    require(type(value.documents) is list)
    originals = cast("list[object]", value.documents)
    require(len(originals) < limits["max_schema_documents"], capacity=True)
    documents: list[JsonValue] = [
        {"name": None, "schema": cast("JsonObject", parameters["schema"])}
    ]
    for original in originals:
        document = record(original, ("name", "schema"))
        require(type(document["name"]) is str and type(document["schema"]) is dict)
        documents.append(cast("JsonObject", document))
    source: JsonObject = {
        "documents": documents,
        "constraints": [0],
        "metadata": {
            "interpretation": PARAMETER_INTERPRETATION,
            "parameter_evaluation": cast("JsonObject", value.parameter_evaluation),
        },
    }
    checked = check_standard_schema_source(
        source, SchemaDomain.PARAMETERS, value.parameter_evaluation
    )
    if checked.reason != "none":
        raise SchemaRefused(checked.reason)
    encoded = json.dumps(
        source, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return PreparedStandardParameters(encoded.encode("ascii"), checked.accounting)


def validate_standard_parameters(
    value: object, prepared: PreparedStandardParameters
) -> SchemaDecision:
    if type(prepared) is not PreparedStandardParameters:
        return SchemaDecision("refused", "unsupported_policy")
    if type(value) is not dict:
        return SchemaDecision("refused", "unsupported_schema")
    source = cast("JsonObject", json.loads(prepared.source_bytes))
    metadata = cast("JsonObject", source["metadata"])
    try:
        bind_policy(SchemaDomain.PARAMETERS, metadata["parameter_evaluation"])
    except SchemaRefused as refused:
        return SchemaDecision("refused", refused.reason)
    return validate_standard_schema(
        cast("dict[object, object]", value),
        source,
        SchemaDomain.PARAMETERS,
        metadata["parameter_evaluation"],
    )
