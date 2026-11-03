import json
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.data import validate_ir_json_object
from kora.ir.json import JsonObject

SCHEMA_FILES: dict[str, str] = {
    "VcsRef": "VcsRef.schema.json",
    "StructuredOutput": "StructuredOutput.schema.json",
    "AgentSession": "AgentSession.schema.json",
    "CommandResult": "CommandResult.schema.json",
}


@dataclass(frozen=True)
class BuiltinSchemaDefinition:
    schema: JsonObject
    file_digest: str


def builtin_schema_definition(
    name: str, revision: str
) -> BuiltinSchemaDefinition | None:
    filename = SCHEMA_FILES.get(name)
    if filename is None or revision != "1":
        return None
    resource = files("kora.ir").joinpath("artifact_schemas", filename)
    raw = resource.read_bytes()
    value: object = json.loads(raw.decode("utf-8"))
    return BuiltinSchemaDefinition(
        validate_ir_json_object(value), sha256(raw).hexdigest()
    )


def builtin_schema(name: str, revision: str) -> JsonObject | None:
    definition = builtin_schema_definition(name, revision)
    return None if definition is None else definition.schema


def contract_schema(name: str, contract: TypeContract) -> JsonObject | None:
    if isinstance(contract, BuiltinTypeContract):
        return builtin_schema(name, contract.builtin_revision)
    return contract.schema_
