from collections.abc import Mapping
from dataclasses import dataclass

from kora.cel_eval.errors import raise_safe
from kora.db.identifiers import is_identifier
from kora.ir.graph_json import (
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.worker_claim_wire import MAX_IMMUTABLE_BYTES

MAX_INPUT_DOCUMENT_BYTES: int = 2 * MAX_IMMUTABLE_BYTES + 64
MAX_INPUT_NATIVE_UNITS: int = 64 + 12 * MAX_INPUT_DOCUMENT_BYTES


class AttemptInputsRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("immutable attempt inputs are unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class BoundAttemptInput:
    name: str
    artifact_version_id: str | None
    path: str | None

    def value(self) -> JsonObject:
        source: JsonObject = (
            {"kind": "absent"}
            if self.artifact_version_id is None
            else {"kind": "artifact", "artifact_version_id": self.artifact_version_id}
        )
        return {"source": source, "path": self.path, "relation": "consumed"}


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAttemptInputs:
    storage: str
    entries: tuple[BoundAttemptInput, ...]

    def claim_map(self) -> JsonObject:
        return {entry.name: entry.value() for entry in self.entries}


def prepare_attempt_inputs(
    declared: JsonObject, versions: Mapping[str, str | None]
) -> PreparedAttemptInputs:
    if declared.keys() != versions.keys():
        raise AttemptInputsRejected()
    entries: list[BoundAttemptInput] = []
    for name, declaration in declared.items():
        if not isinstance(declaration, dict):
            raise AttemptInputsRejected()
        path = declaration.get("path")
        version = versions[name]
        if (path is not None and not isinstance(path, str)) or (
            declaration.get("optional") is not True
            if version is None
            else not is_identifier(version)
        ):
            raise AttemptInputsRejected()
        entries.append(BoundAttemptInput(name, version, path))
    logical: JsonObject = {entry.name: entry.value() for entry in entries}
    members: list[JsonValue] = [
        {"name": entry.name, **entry.value()} for entry in entries
    ]
    try:
        render_object(logical, TextBudget(MAX_IMMUTABLE_BYTES, ascii_only=False))
        storage = render_object(
            {"version": 1, "inputs": members},
            TextBudget(MAX_INPUT_DOCUMENT_BYTES, ascii_only=False),
        )
    except GraphValueRejected:
        raise AttemptInputsRejected() from None
    return PreparedAttemptInputs(storage, tuple(entries))


def admit_input_document(storage: str, maximum: int) -> None:
    if len(storage) > maximum:
        raise AttemptInputsRejected()
    if storage.isascii():
        return
    try:
        for offset in range(0, len(storage), 4096):
            maximum -= len(storage[offset : offset + 4096].encode("utf-8"))
            if maximum < 0:
                raise AttemptInputsRejected()
    except UnicodeError:
        del storage
        raise_safe(AttemptInputsRejected())


def read_attempt_inputs(storage: object, declared: JsonObject) -> PreparedAttemptInputs:
    if not isinstance(storage, str):
        raise AttemptInputsRejected()
    admit_input_document(storage, MAX_INPUT_DOCUMENT_BYTES)
    reader = ObjectReader(storage)
    versions: dict[str, str | None] = {}
    try:
        reader.take('{"version":1,"inputs":[')
        for index, (name, declaration) in enumerate(declared.items()):
            reader.take("," if index else "")
            reader.take('{"name":')
            if reader.string() != name or not isinstance(declaration, dict):
                raise AttemptInputsRejected()
            reader.take(',"source":{"kind":')
            kind = reader.string()
            if kind == "artifact":
                reader.take(',"artifact_version_id":')
                versions[name] = reader.string()
            elif kind == "absent":
                versions[name] = None
            else:
                raise AttemptInputsRejected()
            reader.take('},"path":')
            if declaration.get("path") is None:
                reader.take("null")
            elif reader.string() != declaration["path"]:
                raise AttemptInputsRejected()
            reader.take(',"relation":"consumed"}')
        reader.take("]}")
        prepared = prepare_attempt_inputs(declared, versions)
        if reader.position != len(storage) or prepared.storage != storage:
            raise AttemptInputsRejected()
        return prepared
    except GraphValueRejected:
        raise AttemptInputsRejected() from None
