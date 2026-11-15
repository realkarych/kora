import json
from dataclasses import dataclass, field
from typing import cast

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.ir.json import JsonObject
from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused, bind_policy
from kora.ir.structured_schema_evaluation_policy import (
    PARAMETERS,
    POLICY,
    EvaluationRefused,
    bind_evaluation_policy,
)
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from kora.protocols.effective_artifact_type import EffectiveRefusal, InstalledBehaviors

GraphOutputRefusal = StructuredSchemaPreparationRefusal | EffectiveRefusal


def read_output_policy(value: object) -> JsonObject:
    if type(value) is not dict:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    saved = cast(dict[object, object], value)
    if tuple(saved) != ("policy", "parameters"):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    try:
        if saved["policy"] == POLICY:
            bind_evaluation_policy(saved)
            if tuple(cast(dict[str, object], saved["parameters"])) != tuple(PARAMETERS):
                raise GraphValueRejected(GraphValueFailure.VERSION)
        else:
            bind_policy(SchemaDomain.OUTPUT, saved)
    except (EvaluationRefused, SchemaRefused):
        raise GraphValueRejected(GraphValueFailure.VERSION) from None
    return cast(JsonObject, saved)


@dataclass(frozen=True, slots=True, repr=False)
class OutputPublicationBinding:
    policy_text: str
    behaviors: InstalledBehaviors = field(repr=False)

    def __init__(self, policy: object, behaviors: InstalledBehaviors) -> None:
        if hasattr(self, "policy_text"):
            raise AttributeError("output publication binding is immutable")
        try:
            limits = bind_policy(SchemaDomain.OUTPUT, policy)
        except SchemaRefused:
            raise GraphValueRejected(GraphValueFailure.VERSION) from None
        text = json.dumps(
            {"policy": SchemaDomain.OUTPUT.value, "parameters": dict(limits)},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "policy_text", text)
        object.__setattr__(self, "behaviors", behaviors)

    def policy(self) -> JsonObject:
        return ObjectReader(self.policy_text).object()
