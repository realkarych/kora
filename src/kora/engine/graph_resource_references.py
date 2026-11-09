import json
from dataclasses import dataclass
from typing import Literal, Never, cast

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_command_manifests import (
    MAX_FILES,
    CommandManifestLimits,
    DeclaredCommandManifests,
    build_declared_command_manifests,
    check_declared_command_manifests,
)
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import lexical_depth_valid, unique_object, valid_scalars
from kora.protocols.storage_models import MAX_STORAGE_METADATA, ContentRef

INTERPRETATION = "kora.graph-resource-refs.v1"
MAX_MEMBER_NAME = len("resource_set_digest")
RENDER_COPIES = 3
NATIVE_DEPTH = 8
NATIVE_INTEGER_DIGITS = len(str(COMMAND_RESOURCE_MAX_BYTES))
UNICODE_BYTES_PER_NATIVE_BYTE = 4
UTF8_BYTES_PER_UTF16_BYTE = 2
DECODE_TEXT_COPIES = 3
DECODE_STRUCTURE_UNITS = 10
DECODE_SCANS = 6
_BUILD = object()

NativeResourceEncoding = Literal["utf-8", "utf-16le", "utf-16be"]


@dataclass(frozen=True, slots=True)
class GraphResourceScope:
    namespace_id: str
    graph_id: str
    version_id: str


@dataclass(frozen=True, slots=True)
class GraphResourceReferenceLimits:
    logical: CommandManifestLimits
    source_bytes: int
    scratch: int
    work: int


class _ProtectedGraphResources:
    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("graph resource references are immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("graph resource references are immutable")

    def __getstate__(self) -> Never:
        raise TypeError("graph resource references require explicit access")


class GraphResourceOwner(_ProtectedGraphResources):
    __slots__ = ("scope", "path", "name", "reference")
    scope: GraphResourceScope
    path: tuple[str, ...]
    name: str
    reference: ContentRef

    def __init__(
        self,
        scope: GraphResourceScope,
        path: tuple[str, ...],
        name: str,
        reference: ContentRef,
    ) -> None:
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "reference", reference)


class GraphResourceReferences(_ProtectedGraphResources):
    __slots__ = ("_text",)
    _text: str

    def __init__(self, text: str) -> None:
        if hasattr(self, "_text"):
            del self, text
            raise AttributeError("graph resource references are immutable")
        object.__setattr__(self, "_text", text)

    @property
    def text(self) -> str:
        return self._text


class NativeGraphResourceTransport(_ProtectedGraphResources):
    __slots__ = ("payload", "encoding")
    payload: bytes
    encoding: NativeResourceEncoding

    def __init__(self, payload: bytes, encoding: NativeResourceEncoding) -> None:
        if hasattr(self, "payload"):
            del self, payload, encoding
            raise AttributeError("native graph resources are immutable")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "encoding", encoding)


class PreparedGraphResourceReferences(GraphResourceReferences):
    __slots__ = ("owners",)
    owners: tuple[GraphResourceOwner, ...]

    def __init__(self, text: str, owners: tuple[GraphResourceOwner, ...]) -> None:
        super().__init__(text)
        object.__setattr__(self, "owners", owners)


class ReferenceAdmission:
    def __init__(self, limits: GraphResourceReferenceLimits) -> None:
        self.source: TextBudget = TextBudget(limits.source_bytes)
        self.scratch: TextBudget = TextBudget(limits.scratch)
        self.work: TextBudget = TextBudget(limits.work)

    def inspect(self, count: int) -> None:
        self.source.reserve(count)
        self.work.reserve(count)

    def keep(self, count: int) -> None:
        self.scratch.reserve(count)
        self.work.reserve(count)

    def inspect_text(self, text: str) -> None:
        if type(text) is not str or not text:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        self.inspect(4 * len(text) + 1)

    def render(self, value: JsonObject, max_output_bytes: int | None = None) -> str:
        available = min(self.scratch.remaining, self.work.remaining) // RENDER_COPIES
        if max_output_bytes is not None:
            available = min(available, TextBudget(max_output_bytes).remaining)
        return render_object(value, TextBudget(available))


def _read_native_integer(token: str) -> int:
    if len(token) > NATIVE_INTEGER_DIGITS:
        raise ValueError("invalid graph resource integer")
    return int(token)


def _refuse_native_number(token: str) -> Never:
    raise ValueError("invalid graph resource number")


def _decode_native_resources(
    transport: NativeGraphResourceTransport, admission: ReferenceAdmission
) -> JsonObject:
    payload, encoding = transport.payload, transport.encoding
    if (
        type(payload) is not bytes
        or not payload
        or type(encoding) is not str
        or encoding not in ("utf-8", "utf-16le", "utf-16be")
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    width = len(payload)
    normalized = 0 if encoding == "utf-8" else UTF8_BYTES_PER_UTF16_BYTE * width
    text_units = DECODE_TEXT_COPIES * UNICODE_BYTES_PER_NATIVE_BYTE * width
    admission.inspect(width)
    admission.keep(text_units + DECODE_STRUCTURE_UNITS * width + normalized)
    admission.work.reserve(DECODE_SCANS * width + (normalized or width) + normalized)
    try:
        text = payload.decode(encoding)
        lexical = text.encode("utf-8") if normalized else payload
        if not lexical_depth_valid(lexical, NATIVE_DEPTH):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        del lexical
        value: object = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_int=_read_native_integer,
            parse_float=_refuse_native_number,
            parse_constant=_refuse_native_number,
        )
    except (ValueError, RecursionError):
        raise GraphValueRejected(GraphValueFailure.INVALID) from None
    if type(value) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    document = cast(JsonObject, value)
    if not valid_scalars(document):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return document


def _scope_values(
    scope: GraphResourceScope, admission: ReferenceAdmission
) -> tuple[str, str, str]:
    if type(scope) is not GraphResourceScope:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    values = (scope.namespace_id, scope.graph_id, scope.version_id)
    for value in values:
        admission.inspect_text(value)
    return values


def _index_owners(
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    expected_count: int,
    admission: ReferenceAdmission,
) -> dict[tuple[tuple[str, ...], str], GraphResourceOwner]:
    if type(owners) is not tuple or len(owners) != expected_count:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if expected_count > MAX_FILES:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    target = _scope_values(scope, admission)
    admission.keep(2 + 4 * expected_count)
    indexed: dict[tuple[tuple[str, ...], str], GraphResourceOwner] = {}
    for owner in owners:
        admission.inspect(1)
        if type(owner) is not GraphResourceOwner:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        if _scope_values(owner.scope, admission) != target:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        path, name, reference = owner.path, owner.name, owner.reference
        if type(path) is not tuple or len(path) not in (3, 5):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        if type(reference) is not ContentRef:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        for segment in path:
            admission.inspect_text(segment)
        admission.inspect_text(name)
        key = (path, name)
        if key in indexed:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        indexed[key] = owner
    return indexed


def _logical_commands(
    declared: DeclaredCommandManifests, admission: ReferenceAdmission
) -> tuple[list[JsonObject], int]:
    text = declared.text
    admission.inspect(len(text))
    admission.keep(3 * len(text))
    value = ObjectReader(text).object()
    commands = cast(list[JsonObject], value["commands"])
    admission.keep(1)
    nonempty: list[JsonObject] = []
    count = 0
    for entry in commands:
        admission.work.reserve(1)
        files = cast(list[JsonObject], entry["resources"])
        if files:
            admission.keep(1)
            nonempty.append(entry)
            count += len(files)
    return nonempty, count


def _retain_owner(
    owner: GraphResourceOwner, path_work: int, admission: ReferenceAdmission
) -> None:
    scope = owner.scope
    scope_bytes = (
        4 * (len(scope.namespace_id) + len(scope.graph_id) + len(scope.version_id)) + 3
    )
    name_bytes = 4 * len(owner.name) + 1
    admission.keep(
        8
        + len(owner.path)
        + scope_bytes
        + path_work
        + name_bytes
        + MAX_STORAGE_METADATA
    )


def _reference_document(
    declared: DeclaredCommandManifests,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    admission: ReferenceAdmission,
    ordered: list[GraphResourceOwner] | None,
) -> JsonObject:
    logical, count = _logical_commands(declared, admission)
    indexed = _index_owners(scope, owners, count, admission)
    admission.keep(3 + len(logical))
    if ordered is not None:
        admission.keep(2 * count)
    commands: list[JsonValue] = []
    for entry in logical:
        path = cast(list[str], entry["path"])
        path_work = sum(4 * len(segment) + 1 for segment in path)
        admission.work.reserve(path_work)
        admission.keep(4 + len(path))
        path_key = tuple(path)
        resources: list[JsonValue] = []
        for file in cast(list[JsonObject], entry["resources"]):
            name = cast(str, file["name"])
            admission.work.reserve(path_work + 4 * len(name) + 1)
            owner = indexed.pop((path_key, name), None)
            if owner is None:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            reference = owner.reference
            admission.inspect(MAX_STORAGE_METADATA)
            if (
                reference.address.payload_digest != file["sha256"]
                or reference.total_size != file["size"]
            ):
                raise GraphValueRejected(GraphValueFailure.INVALID)
            admission.keep(2 * MAX_STORAGE_METADATA + 6)
            storage_ref = cast(JsonObject, reference.model_dump(mode="json"))
            resources.append({**file, "storage_ref": storage_ref})
            if ordered is not None:
                _retain_owner(owner, path_work, admission)
                ordered.append(owner)
        commands.append(
            {
                "path": entry["path"],
                "resource_set_digest": entry["resource_set_digest"],
                "resources": resources,
            }
        )
    if indexed:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return {"interpretation": INTERPRETATION, "commands": commands}


def _matches(
    expected: JsonValue, actual: object, admission: ReferenceAdmission
) -> bool:
    admission.inspect(1)
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        mapping = cast(dict[object, object], actual)
        if len(expected) != len(mapping):
            return False
        for key, value in mapping.items():
            if type(key) is not str or len(key) > MAX_MEMBER_NAME:
                return False
            admission.inspect_text(key)
            if key not in expected or not _matches(expected[key], value, admission):
                return False
        return True
    if isinstance(expected, list):
        sequence = cast(list[object], actual)
        return len(expected) == len(sequence) and all(
            _matches(item, other, admission)
            for item, other in zip(expected, sequence, strict=True)
        )
    if isinstance(expected, str):
        if len(expected) != len(cast(str, actual)):
            return False
        admission.inspect_text(expected)
    return expected == actual


def _references_result(
    ir_text: str,
    saved_logical: object,
    saved_physical: object,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    limits: GraphResourceReferenceLimits,
    max_output_bytes: int | None = None,
) -> GraphResourceReferences | GraphValueFailure:
    try:
        admission = ReferenceAdmission(limits)
        if type(saved_physical) is NativeGraphResourceTransport:
            saved_physical = _decode_native_resources(saved_physical, admission)
        ordered: list[GraphResourceOwner] | None = None
        if saved_logical is _BUILD:
            declared = build_declared_command_manifests(ir_text, limits=limits.logical)
            admission.keep(3)
            ordered = []
        else:
            declared = check_declared_command_manifests(
                ir_text, saved_logical, limits=limits.logical
            )
        expected = _reference_document(declared, scope, owners, admission, ordered)
        if saved_physical is not _BUILD and not _matches(
            expected, saved_physical, admission
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        rendered = admission.render(expected, max_output_bytes)
        if ordered is None:
            return GraphResourceReferences(rendered)
        return PreparedGraphResourceReferences(rendered, tuple(ordered))
    except GraphValueRejected as rejected:
        return rejected.reason


def build_graph_resource_references(
    ir_text: str,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    *,
    limits: GraphResourceReferenceLimits,
    max_output_bytes: int | None = None,
) -> PreparedGraphResourceReferences:
    result = _references_result(
        ir_text, _BUILD, _BUILD, scope, owners, limits, max_output_bytes
    )
    if isinstance(result, PreparedGraphResourceReferences):
        return result
    reason = (
        result if isinstance(result, GraphValueFailure) else GraphValueFailure.INVALID
    )
    del ir_text, scope, owners, max_output_bytes
    raise_safe(GraphValueRejected(reason))


def check_graph_resource_references(
    ir_text: str,
    saved_logical: object,
    saved_physical: object,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    *,
    limits: GraphResourceReferenceLimits,
) -> GraphResourceReferences:
    result = _references_result(
        ir_text, saved_logical, saved_physical, scope, owners, limits
    )
    if isinstance(result, GraphResourceReferences):
        return result
    del ir_text, saved_logical, saved_physical, scope, owners
    raise_safe(GraphValueRejected(result))
