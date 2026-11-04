import hashlib
import json
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import cast

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, TextBudget
from kora.ir.agent_session import agent_session_revision_descriptor
from kora.ir.data import validate_ir_json_object
from kora.ir.json import JsonObject

POLICY_BYTES = 1174
SCHEMA_BYTES = 5855
VIEW_BYTES = POLICY_BYTES + SCHEMA_BYTES


def published_object(resource: Traversable, size: int, digest: str) -> JsonObject:
    try:
        with resource.open("rb") as stream:
            raw = stream.read(size + 1)
    except OSError:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    try:
        value: object = json.loads(raw.decode("utf-8"))
        return dict(validate_ir_json_object(value))
    except ValueError:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None


def agent_session_checker(budget: TextBudget) -> JsonObject:
    budget.reserve(VIEW_BYTES)
    root = files("kora.ir").joinpath("artifact_schemas")
    descriptor = agent_session_revision_descriptor()
    policy = published_object(
        root.joinpath("AgentSession.cel-policy.json"),
        POLICY_BYTES,
        cast(str, descriptor["cel_policy_sha256"]),
    )
    schema = published_object(
        root.joinpath("AgentSession.cel.schema.json"),
        SCHEMA_BYTES,
        cast(str, policy["cel_schema_sha256"]),
    )
    return {
        "kind": "published_view",
        "name": "AgentSession",
        "policy": policy,
        "schema": schema,
    }
