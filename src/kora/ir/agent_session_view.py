import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import cast

from kora.ir.agent_session import agent_session_revision_descriptor
from kora.ir.data import validate_ir_json_object
from kora.ir.json import JsonObject

POLICY_BYTES = 1174
SCHEMA_BYTES = 5855
VIEW_BYTES = POLICY_BYTES + SCHEMA_BYTES


class AgentSessionViewUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("published AgentSession view unavailable")


@dataclass(frozen=True, slots=True)
class AgentSessionView:
    policy: JsonObject
    schema: JsonObject


def published_object(resource: Traversable, size: int, digest: str) -> JsonObject:
    try:
        with resource.open("rb") as stream:
            raw = stream.read(size + 1)
    except OSError:
        raise AgentSessionViewUnavailable() from None
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise AgentSessionViewUnavailable()
    try:
        value: object = json.loads(raw.decode("utf-8"))
        return dict(validate_ir_json_object(value))
    except ValueError:
        raise AgentSessionViewUnavailable() from None


def read_agent_session_view() -> AgentSessionView:
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
    return AgentSessionView(policy, schema)
