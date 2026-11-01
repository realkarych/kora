from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Literal, Never, Protocol, cast

from jsonschema import Draft202012Validator, validators

from kora.ir.structured_schema_assertions import type_matches
from kora.ir.structured_schema_cursors import (
    NativeCursor,
    NativeObjectIndex,
    arena_cursor,
)
from kora.ir.structured_schema_evaluation_policy import (
    PARAMETERS,
    StructuredSchemaEvaluationPolicy,
    refuse,
)
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import PreparedStructuredSchemaInput, SchemaNode

_HASH_WORK = 791
_EVOLVE_WORK: int = 128 + 16 * _HASH_WORK
_MAX_DEPTH: int = PARAMETERS["max_evaluation_depth"]
_END: object = object()
_KEYWORDS: tuple[str, ...] = (
    "$dynamicRef",
    "$ref",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "contains",
    "dependentRequired",
    "dependentSchemas",
    "else",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "if",
    "items",
    "maxContains",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minContains",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "prefixItems",
    "properties",
    "propertyNames",
    "required",
    "then",
    "type",
    "uniqueItems",
)


@dataclass(slots=True, repr=False, eq=False)
class _Context:
    meter: EvaluationMeter
    prepared: PreparedStructuredSchemaInput
    root: object
    status: Literal["active", "closed"]

    def __repr__(self) -> str:
        return "_Context()"


@dataclass(slots=True, repr=False, eq=False)
class _Frame:
    context: _Context
    position: int
    value: NativeCursor
    depth: int
    index: NativeObjectIndex | None
    remaining_handlers: int

    def __repr__(self) -> str:
        return "_Frame()"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class _BoundKeyword:
    frame: _Frame
    node: int

    def __repr__(self) -> str:
        return "_BoundKeyword()"


class _ClosedResolver:
    __slots__ = ()

    def __repr__(self) -> str:
        return "_ClosedResolver()"


class _Mismatch(Exception):
    def __str__(self) -> str:
        return "schema mismatch"


class _UnavailableHandler(RuntimeError):
    def __str__(self) -> str:
        return "schema adapter handler unavailable"


class _Engine(Protocol):
    _resolver: object
    _ref_resolver: object

    def evolve(
        self, *, schema: dict[str, _BoundKeyword], _resolver: _ClosedResolver
    ) -> _Engine: ...

    def iter_errors(self, instance: NativeCursor) -> Generator[object, None, None]: ...


_Handler = Callable[
    [_Engine, _BoundKeyword, NativeCursor, dict[str, _BoundKeyword]], None
]


class _EngineFactory(Protocol):
    def __call__(
        self,
        schema: dict[str, _BoundKeyword],
        *,
        _resolver: _ClosedResolver,
        resolver: None,
    ) -> _Engine: ...


class _CreateEngine(Protocol):
    def __call__(
        self,
        *,
        meta_schema: dict[str, object],
        validators: dict[str, _Handler],
        version: None,
        type_checker: object,
    ) -> _EngineFactory: ...


class _DraftMetadata(Protocol):
    META_SCHEMA: dict[str, object]
    TYPE_CHECKER: object


def _keyword_index(key: str) -> int:
    position = 0
    while position < 35:
        if key == _KEYWORDS[position]:
            return position
        position += 1
    return -1


def _view(
    frame: _Frame,
    node: SchemaNode,
    arena: tuple[SchemaNode, ...],
    meter: EvaluationMeter,
) -> dict[str, _BoundKeyword]:
    meter.reserve()
    members = node.members
    meter.reserve()
    count = len(members)
    meter.reserve(109)
    meter.reserve(108, entries=36)
    slots: list[int | None] = []
    try:
        cursor = 0
        while cursor < 35:
            slots.append(None)
            cursor += 1
        meter.reserve()
        executable = 0
        meter.reserve()
        cursor = 0
        while True:
            meter.reserve()
            if cursor == count:
                break
            meter.reserve()
            row = members[cursor]
            meter.reserve()
            key_id = row[0]
            meter.reserve()
            value_id = row[1]
            meter.reserve()
            key_node = arena[key_id]
            meter.reserve()
            key = cast("str", key_node.value)
            meter.reserve(1 + 35 * (1 + len(key) + 20))
            keyword = _keyword_index(key)
            meter.reserve()
            if keyword >= 0:
                meter.reserve()
                slots[keyword] = value_id
                meter.reserve()
                executable += 1
            meter.reserve()
            cursor += 1
        meter.reserve(1 + executable * (4 + _HASH_WORK), entries=1 + 3 * executable)
        view: dict[str, _BoundKeyword] = {}
        meter.reserve()
        cursor = 0
        while True:
            meter.reserve()
            if cursor == 35:
                break
            meter.reserve()
            selected = slots[cursor]
            meter.reserve()
            if selected is not None:
                meter.reserve()
                keyword = _KEYWORDS[cursor]
                view[keyword] = _BoundKeyword(frame, selected)
            meter.reserve()
            cursor += 1
        return view
    finally:
        count = len(slots)
        cursor = 0
        while cursor < count:
            slots[cursor] = None
            cursor += 1
        del slots


def _drive(
    engine: _Engine, value: NativeCursor, count: int, meter: EvaluationMeter
) -> bool:
    meter.reserve()
    factory = engine.iter_errors
    meter.reserve(3, entries=1)
    iterator = factory(value)
    try:
        meter.reserve()
        if count:
            meter.reserve(20)
        else:
            meter.reserve(2)
        result = next(iterator, _END)
        meter.reserve()
        if result is not _END:
            raise RuntimeError("schema adapter iterator yielded")
        return True
    finally:
        iterator.close()


def _decide(
    context: _Context,
    position: int,
    value: NativeCursor,
    parent_depth: int,
    parent_engine: _Engine | None,
    resolver: _ClosedResolver,
    meter: EvaluationMeter,
) -> bool:
    meter.reserve()
    depth = parent_depth + 1
    meter.reserve()
    if depth > _MAX_DEPTH:
        refuse("evaluation_exhausted")
    meter.reserve(9, entries=1)
    frame = _Frame(context, position, value, depth, None, 0)
    try:
        meter.reserve()
        prepared = context.prepared
        meter.reserve()
        positions = cast(
            "tuple[tuple[int, int, int, int, int], ...]",
            object.__getattribute__(prepared, "_positions"),
        )
        meter.reserve()
        original = positions[position]
        meter.reserve()
        node_id = original[1]
        meter.reserve()
        arena = cast(
            "tuple[SchemaNode, ...]", object.__getattribute__(prepared, "_arena")
        )
        meter.reserve()
        node = arena[node_id]
        meter.reserve()
        kind = node.kind
        meter.reserve()
        if kind == "scalar":
            meter.reserve()
            boolean = node.value
            meter.reserve()
            boolean_kind = type(boolean)
            meter.reserve()
            if boolean_kind is not bool:
                refuse()
            return cast("bool", boolean)
        meter.reserve()
        if kind != "object":
            refuse()
        view = _view(frame, node, arena, meter)
        count = len(view)
        meter.reserve()
        frame.remaining_handlers = count
        meter.reserve()
        if parent_engine is None:
            meter.reserve(128 + (2 * count + 8) * _HASH_WORK, entries=34 + 2 * count)
            engine = _CLASS(view, _resolver=resolver, resolver=None)
        else:
            meter.reserve()
            evolve = parent_engine.evolve
            meter.reserve(_EVOLVE_WORK, entries=32)
            meter.reserve(128 + (2 * count + 8) * _HASH_WORK, entries=34 + 2 * count)
            engine = evolve(schema=view, _resolver=resolver)
        meter.reserve()
        engine_kind = type(engine)
        meter.reserve()
        if engine_kind is not cast("type[_Engine]", _CLASS):
            raise RuntimeError("schema adapter engine identity changed")
        meter.reserve()
        actual_resolver: object = object.__getattribute__(engine, "_resolver")
        meter.reserve()
        if actual_resolver is not resolver:
            raise RuntimeError("schema adapter resolver identity changed")
        meter.reserve()
        legacy_resolver: object = object.__getattribute__(engine, "_ref_resolver")
        meter.reserve()
        if legacy_resolver is not None:
            raise RuntimeError("schema adapter legacy resolver enabled")
        return _drive(engine, value, count, meter)
    except _Mismatch:
        return False
    finally:
        del frame


def _finish(frame: _Frame, meter: EvaluationMeter) -> None:
    meter.reserve()
    remaining = frame.remaining_handlers
    meter.reserve()
    remaining -= 1
    frame.remaining_handlers = remaining
    meter.reserve()
    if remaining:
        meter.reserve(19)
    else:
        meter.reserve()


def _mismatch(meter: EvaluationMeter) -> Never:
    meter.reserve(2, entries=1)
    raise _Mismatch from None


def _type(
    _engine: _Engine,
    argument: _BoundKeyword,
    instance: NativeCursor,
    _schema: dict[str, _BoundKeyword],
) -> None:
    frame = argument.frame
    context = frame.context
    meter = context.meter
    meter.reserve()
    prepared = context.prepared
    meter.reserve()
    arena = cast("tuple[SchemaNode, ...]", object.__getattribute__(prepared, "_arena"))
    meter.reserve()
    node = argument.node
    original = arena_cursor(arena, node, meter)
    matched = type_matches(instance, original, meter)
    meter.reserve()
    if not matched:
        _mismatch(meter)
    _finish(frame, meter)


def _reference(
    engine: _Engine,
    argument: _BoundKeyword,
    instance: NativeCursor,
    _schema: dict[str, _BoundKeyword],
) -> None:
    frame = argument.frame
    context = frame.context
    meter = context.meter
    meter.reserve()
    position = frame.position
    meter.reserve()
    occurrence = argument.node
    meter.reserve()
    prepared = context.prepared
    meter.reserve()
    references = cast(
        "tuple[tuple[int, int, int, int], ...]",
        object.__getattribute__(prepared, "_references"),
    )
    count = len(references)
    meter.reserve(1 if not count else 2 + count.bit_length())
    low, high = 0, count
    while low < high:
        middle = (low + high) // 2
        if references[middle][0] < position:
            low = middle + 1
        else:
            high = middle
    first = low if low < count and references[low][0] == position else count
    meter.reserve()
    attempt = 0
    while True:
        meter.reserve()
        if attempt == 2:
            raise RuntimeError("schema adapter reference occurrence missing")
        meter.reserve()
        if first + attempt >= count:
            raise RuntimeError("schema adapter reference occurrence missing")
        meter.reserve()
        row = references[first + attempt]
        meter.reserve()
        source = row[0]
        meter.reserve()
        if source != position:
            raise RuntimeError("schema adapter reference occurrence missing")
        meter.reserve()
        original = row[2]
        meter.reserve()
        if original == occurrence:
            meter.reserve()
            target = row[3]
            break
        meter.reserve()
        attempt += 1
    meter.reserve()
    depth = frame.depth
    meter.reserve()
    resolver = cast("_ClosedResolver", object.__getattribute__(engine, "_resolver"))
    meter.reserve()
    matched = _decide(context, target, instance, depth, engine, resolver, meter)
    meter.reserve()
    if not matched:
        _mismatch(meter)
    _finish(frame, meter)


def _noop(
    _engine: _Engine,
    argument: _BoundKeyword,
    _instance: NativeCursor,
    _schema: dict[str, _BoundKeyword],
) -> None:
    frame = argument.frame
    context = frame.context
    meter = context.meter
    _finish(frame, meter)


def _unavailable(
    _engine: _Engine,
    argument: _BoundKeyword,
    _instance: NativeCursor,
    _schema: dict[str, _BoundKeyword],
) -> None:
    frame = argument.frame
    context = frame.context
    meter = context.meter
    meter.reserve(entries=1)
    raise _UnavailableHandler from None


def decide_prepared_schema(
    prepared: object,
    original_position: object,
    value: object,
    policy: object,
    meter: EvaluationMeter,
) -> bool:
    meter.reserve()
    policy_kind = type(policy)
    meter.reserve()
    if policy_kind is not StructuredSchemaEvaluationPolicy:
        refuse("unsupported_policy")
    meter.reserve()
    prepared_kind = type(prepared)
    meter.reserve()
    if prepared_kind is not PreparedStructuredSchemaInput:
        refuse()
    meter.reserve()
    value_kind = type(value)
    meter.reserve()
    if value_kind is not NativeCursor:
        refuse()
    meter.reserve()
    position_kind = type(original_position)
    meter.reserve()
    if position_kind is not int:
        refuse()
    position = cast("int", original_position)
    meter.reserve()
    positions = cast(
        "tuple[tuple[int, int, int, int, int], ...]",
        object.__getattribute__(prepared, "_positions"),
    )
    meter.reserve()
    count = len(positions)
    meter.reserve()
    if position < 0:
        refuse()
    meter.reserve()
    if position >= count:
        refuse()
    meter.reserve()
    cursor = cast("NativeCursor", value)
    root = cursor.value
    meter.reserve()
    meter.reserve(5, entries=1)
    context = _Context(
        meter, cast("PreparedStructuredSchemaInput", prepared), root, "active"
    )
    try:
        meter.reserve(128, entries=16)
        resolver = _ClosedResolver()
        return _decide(context, position, cursor, 0, None, resolver, meter)
    finally:
        context.status = "closed"


_HANDLERS: dict[str, _Handler] = {
    "$dynamicRef": _reference,
    "$ref": _reference,
    "additionalProperties": _unavailable,
    "allOf": _unavailable,
    "anyOf": _unavailable,
    "const": _unavailable,
    "contains": _unavailable,
    "dependentRequired": _unavailable,
    "dependentSchemas": _unavailable,
    "else": _noop,
    "enum": _unavailable,
    "exclusiveMaximum": _unavailable,
    "exclusiveMinimum": _unavailable,
    "if": _unavailable,
    "items": _unavailable,
    "maxContains": _noop,
    "maxItems": _unavailable,
    "maxLength": _unavailable,
    "maxProperties": _unavailable,
    "maximum": _unavailable,
    "minContains": _noop,
    "minItems": _unavailable,
    "minLength": _unavailable,
    "minProperties": _unavailable,
    "minimum": _unavailable,
    "multipleOf": _unavailable,
    "not": _unavailable,
    "oneOf": _unavailable,
    "prefixItems": _unavailable,
    "properties": _unavailable,
    "propertyNames": _unavailable,
    "required": _unavailable,
    "then": _noop,
    "type": _type,
    "uniqueItems": _unavailable,
}
_DRAFT: _DraftMetadata = cast("_DraftMetadata", Draft202012Validator)
_CREATE: _CreateEngine = cast(
    "_CreateEngine", object.__getattribute__(validators, "create")
)
_CLASS: _EngineFactory = _CREATE(
    meta_schema=_DRAFT.META_SCHEMA,
    validators=_HANDLERS,
    version=None,
    type_checker=_DRAFT.TYPE_CHECKER,
)
