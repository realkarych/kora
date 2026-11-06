from collections.abc import Collection, Iterator
from dataclasses import dataclass
from functools import cmp_to_key
from hashlib import sha256
from typing import Never, cast
from unicodedata import is_normalized

from pydantic import MISSING

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    render_object,
)
from kora.engine.graph_types import read_document
from kora.ir.config import CommandConfig
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import encode_snapshot
from kora.ir.topology import CommandNode

INTERPRETATION = "kora.command-resources.v1"
EMPTY_DIGEST = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
MAX_FILES = 64
MAX_CONTENT_BYTES = 1_048_576
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class CommandManifestLimits:
    ir_bytes: int
    scratch: int
    work: int


class DeclaredCommandManifests:
    __slots__ = ("_text",)
    _text: str

    def __init__(self, text: str) -> None:
        object.__setattr__(self, "_text", text)

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("command resource manifests are immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("command resource manifests are immutable")

    @property
    def text(self) -> str:
        return self._text

    def __getstate__(self) -> Never:
        raise TypeError("command resource manifests require explicit access")


class ManifestAdmission:
    def __init__(self, limits: CommandManifestLimits) -> None:
        self.source: TextBudget = TextBudget(limits.ir_bytes)
        self.scratch: TextBudget = TextBudget(limits.scratch)
        self.work: TextBudget = TextBudget(limits.work)
        self.files: int = 0
        self.content: int = 0

    def keep(self, units: int) -> None:
        self.scratch.reserve(units)
        self.work.reserve(units)

    def add_file(self, size: int) -> None:
        self.files += 1
        self.content += size
        if self.files > MAX_FILES or self.content > MAX_CONTENT_BYTES:
            raise GraphValueRejected(GraphValueFailure.INVALID)

    def ordered(self, values: Collection[bytes]) -> list[bytes]:
        def compare(left: bytes, right: bytes) -> int:
            self.work.reserve(2 * min(len(left), len(right)) + 1)
            return int(left > right) - int(left < right)

        count = len(values)
        self.keep(3 * count)
        self.work.reserve(count * (64 + 4 * count.bit_length()))
        return sorted(values, key=cmp_to_key(compare))


class ManifestTextBudget(TextBudget):
    def __init__(self, admission: ManifestAdmission) -> None:
        super().__init__(
            min(admission.scratch.remaining, admission.work.remaining) // 3
        )
        self.admission: ManifestAdmission = admission

    def reserve(self, count: int) -> None:
        super().reserve(count)
        self.admission.keep(3 * count)


def _admit_source(text: str, admission: ManifestAdmission) -> None:
    if type(text) is not str or not text.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if len(text) > admission.source.remaining:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    admission.keep(3 * len(text))
    admission.work.reserve(len(text))
    quoted = False
    escaped = False
    width = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char in "0123456789.eE-+":
            width += 1
        else:
            if width:
                admission.work.reserve(2 * width * width)
                width = 0
            quoted = char == '"'
    admission.work.reserve(2 * width * width)


def _commands(
    document: GraphDocument, admission: ManifestAdmission
) -> Iterator[tuple[list[JsonValue], CommandConfig]]:
    for name, node in document.graph.nodes.items():
        admission.work.reserve(1)
        if isinstance(node, CommandNode):
            admission.keep(3)
            yield ["graph", "nodes", name], node.config
    bodies = document.graph.subgraphs
    if bodies is MISSING:
        return
    for body, definition in bodies.items():
        admission.work.reserve(1)
        for name, node in definition.nodes.items():
            admission.work.reserve(1)
            if isinstance(node, CommandNode):
                admission.keep(5)
                yield ["graph", "subgraphs", body, "nodes", name], node.config


def _resource_manifest(
    config: CommandConfig, admission: ManifestAdmission
) -> tuple[str | None, list[JsonValue]]:
    resources = config.resources
    digest = config.resource_set_digest
    if resources is MISSING or not resources:
        if digest is not MISSING and (resources is MISSING or digest != EMPTY_DIGEST):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        admission.keep(1)
        return None, []
    if digest is MISSING:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    admission.keep(1)
    by_name: dict[bytes, JsonObject] = {}
    jcs_bytes = 2
    for resource in resources:
        admission.add_file(resource.size)
        name = resource.name
        length = len(name)
        admission.keep(20 * length + 6)
        if not name.isascii():
            admission.work.reserve(length * length)
        try:
            key = name.encode("utf-8")
        except UnicodeEncodeError:
            raise GraphValueRejected(GraphValueFailure.INVALID) from None
        if not is_normalized("NFC", name) or key in by_name:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        by_name[key] = {
            "name": name,
            "sha256": resource.sha256,
            "size": resource.size,
            "executable": resource.executable,
        }
        jcs_bytes += 160 + 2 * len(key)
    ordered = admission.ordered(by_name.keys())
    admission.keep(2 * jcs_bytes + len(ordered))
    admission.work.reserve(jcs_bytes)
    files: list[JsonValue] = [by_name[key] for key in ordered]
    expected = "sha256:" + sha256(encode_snapshot(files)).hexdigest()
    if digest != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return digest, files


def _derive(text: str, admission: ManifestAdmission) -> JsonObject:
    _admit_source(text, admission)
    document = read_document(text, admission.source)
    admission.keep(2)
    records: dict[bytes, JsonObject] = {}
    for path, config in _commands(document, admission):
        width = sum(len(cast(str, segment)) + 3 for segment in path) + 2
        admission.keep(3 * width + 4)
        key = encode_snapshot(path)
        digest, files = _resource_manifest(config, admission)
        records[key] = {"path": path, "resource_set_digest": digest, "resources": files}
    ordered = admission.ordered(records.keys())
    admission.keep(len(ordered) + 2)
    return {
        "interpretation": INTERPRETATION,
        "commands": [records[key] for key in ordered],
    }


def _matches(expected: JsonValue, saved: object, work: TextBudget) -> bool:
    work.reserve(1)
    if type(saved) is not type(expected):
        return False
    if isinstance(expected, dict):
        mapping = cast(dict[object, object], saved)
        return len(expected) == len(mapping) and all(
            _matches(key, other_key, work) and _matches(value, other_value, work)
            for (key, value), (other_key, other_value) in zip(
                expected.items(), mapping.items(), strict=True
            )
        )
    if isinstance(expected, list):
        sequence = cast(list[object], saved)
        return len(expected) == len(sequence) and all(
            _matches(value, other, work)
            for value, other in zip(expected, sequence, strict=True)
        )
    if isinstance(expected, str):
        if len(expected) != len(cast(str, saved)):
            return False
        work.reserve(len(expected))
    return expected == saved


def _manifest_result(
    ir_text: str, saved: object, limits: CommandManifestLimits
) -> DeclaredCommandManifests | GraphValueFailure:
    try:
        admission = ManifestAdmission(limits)
        expected = _derive(ir_text, admission)
        if saved is not _ABSENT and not _matches(expected, saved, admission.work):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        return DeclaredCommandManifests(
            render_object(expected, ManifestTextBudget(admission))
        )
    except GraphValueRejected as rejected:
        return rejected.reason


def build_declared_command_manifests(
    ir_text: str, *, limits: CommandManifestLimits
) -> DeclaredCommandManifests:
    result = _manifest_result(ir_text, _ABSENT, limits)
    if isinstance(result, DeclaredCommandManifests):
        return result
    del ir_text
    raise_safe(GraphValueRejected(result))


def check_declared_command_manifests(
    ir_text: str, saved: object, *, limits: CommandManifestLimits
) -> DeclaredCommandManifests:
    result = _manifest_result(ir_text, saved, limits)
    if isinstance(result, DeclaredCommandManifests):
        return result
    del ir_text, saved
    raise_safe(GraphValueRejected(result))
