import json
from importlib.resources import files

from kora.ir.data import validate_ir_json_object
from kora.ir.json import JsonObject

SCHEMA_FILES: dict[str, str] = {
    "VcsRef": "VcsRef.schema.json",
    "StructuredOutput": "StructuredOutput.schema.json",
    "AgentSession": "AgentSession.schema.json",
    "CommandResult": "CommandResult.schema.json",
}


def builtin_schema(name: str, revision: str) -> JsonObject | None:
    filename = SCHEMA_FILES.get(name)
    if filename is None or revision != "1":
        return None
    resource = files("kora.ir").joinpath("artifact_schemas", filename)
    value: object = json.loads(resource.read_text(encoding="utf-8"))
    return validate_ir_json_object(value)
