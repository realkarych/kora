import hashlib
from typing import cast

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_native_sources import VIEW_BYTES, agent_session_checker
from kora.engine.graph_output_sources import object_at, output_path
from kora.engine.graph_output_values import (
    GraphOutputContract,
    OutputInputs,
    admit_output_inputs,
    general_closure,
    saved_array,
)
from kora.engine.graph_types import read_document, read_graph_type_contracts
from kora.ir.json import JsonObject
from kora.ir.output_schemas import validate_output_schemas


def native_output_type(raw: JsonObject, path: list[str]) -> str:
    node = object_at(raw, path[:-2])
    declaration = object_at(raw, path)
    name = declaration["type"]
    if "schema" in declaration or not isinstance(name, str):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if not (
        node["kind"] == "agent"
        and (
            name == "AgentSession"
            and path[-1] == "session"
            or name == "VcsRef"
            and path[-1] != "session"
        )
        or node["kind"] == "command"
        and name == "CommandResult"
        and path[-1] == "result"
    ):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    return name


def derive_native_output(inputs: OutputInputs, saved_size: int = 0) -> str:
    document = read_document(inputs.ir_text, TextBudget(len(inputs.ir_text)))
    records = cast(list[JsonObject], saved_array(inputs.types_text))
    read_graph_type_contracts(
        inputs.ir_text,
        records,
        max_encoded_bytes=len(inputs.ir_text) + len(inputs.types_text),
    )
    if not validate_output_schemas(document).output_schemas_valid:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    path = output_path(saved_array(inputs.path_text))
    name = native_output_type(ObjectReader(inputs.ir_text).object(), path)
    by_name = {cast(str, record["name"]): record for record in records}
    if name not in by_name or by_name[name]["builtin_revision"] != "1":
        raise GraphValueRejected(GraphValueFailure.VERSION)
    budget = TextBudget(inputs.remaining)
    checker: JsonObject
    binding: str | None
    if name == "AgentSession":
        checker = agent_session_checker(TextBudget(inputs.remaining - saved_size))
        budget.reserve(VIEW_BYTES)
        binding = "kora.agent-session-view.v1"
    else:
        checker = {"kind": "declared_type", "name": name}
        binding = None
    return render_object(
        {
            "execution_semantics": "kora.execution.v1",
            "declared_type": name,
            "type_contracts": general_closure([name], [], by_name),
            "inline_schema": None,
            "checker_binding": binding,
            "checker": checker,
            "validation": {"kind": "published_builtin", "name": name},
        },
        budget,
    )


def build_graph_native_output_contract(
    ir_text: str, type_contracts: object, path: object, *, max_encoded_bytes: int
) -> GraphOutputContract:
    inputs = admit_output_inputs(
        ir_text, type_contracts, path, TextBudget(max_encoded_bytes)
    )
    text = derive_native_output(inputs)
    return GraphOutputContract(text, hashlib.sha256(text.encode("ascii")).hexdigest())


def read_graph_native_output_contract(
    ir_text: str,
    type_contracts: object,
    path: object,
    value: object,
    digest: str,
    *,
    max_encoded_bytes: int,
) -> GraphOutputContract:
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
    expected = derive_native_output(inputs, len(text))
    if text != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphOutputContract(text, digest)
