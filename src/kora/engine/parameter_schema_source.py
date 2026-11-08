import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from kora.cel_eval.errors import raise_safe
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceRejected,
    ParameterSchemaArena,
    valid_parameter_reference_budget,
)
from kora.engine.parameter_schema_scalars import (
    ParameterScalarFailure,
    ParameterScalarRejected,
    prepare_parameter_schema_scalar,
)
from kora.ir.structured_schema_input import SchemaNode
from kora.ir.structured_schema_work import merge_sort

_FRAME_ENTRIES: int = 8
_ROW_ENTRIES: int = 8
_PAIR_ENTRIES: int = 8
_LIST_ENTRIES: int = 5


class ParameterSchemaSourceFailure(StrEnum):
    INVALID = "invalid pinned parameter schema source"
    CAPACITY = "pinned parameter schema source capacity exceeded"


class ParameterSchemaSourceRejected(ValueError):
    def __init__(self, reason: ParameterSchemaSourceFailure) -> None:
        self.reason: ParameterSchemaSourceFailure = reason
        super().__init__(reason.value)


@dataclass(slots=True, repr=False)
class _ArrayFrame:
    source: list[object]
    count: int
    offset: int = 0
    children: list[int] = field(default_factory=list[int])


@dataclass(slots=True, repr=False)
class _ObjectFrame:
    source: dict[object, object]
    rows: list[tuple[str, object]]
    offset: int = 0
    members: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    key: int | None = None
    maximum_key: int = 0


_Frame = _ArrayFrame | _ObjectFrame


class _Builder:
    def __init__(
        self,
        budget: ParameterReferenceBudget,
        max_container_depth: int,
        max_integer_bits: int,
    ) -> None:
        self.budget: ParameterReferenceBudget = budget
        self.max_container_depth: int = max_container_depth
        self.max_integer_bits: int = max_integer_bits
        self.nodes: list[SchemaNode] = []
        self.frames: list[_Frame] = []

    def _rows(
        self, source: dict[object, object], *, named_roots: bool = False
    ) -> list[tuple[str, object]]:
        count = len(source)
        self.budget.reserve(1 + 2 * count, entries=_ROW_ENTRIES * count)
        rows: list[tuple[str, object]] = []
        for key, value in source.items():
            if len(rows) == count or type(key) is not str:
                raise ParameterSchemaSourceRejected(
                    ParameterSchemaSourceFailure.INVALID
                )
            row: tuple[str, object] = (key, value)
            if named_roots and type(value) is not dict:
                raise ParameterSchemaSourceRejected(
                    ParameterSchemaSourceFailure.INVALID
                )
            rows.append(row)
        if len(rows) != count:
            raise ParameterSchemaSourceRejected(ParameterSchemaSourceFailure.INVALID)
        merge_sort(rows, lambda row: row[0], self.budget)
        return rows

    def _open(self, source: dict[object, object] | list[object]) -> None:
        self.budget.reserve()
        depth = len(self.frames) + 1
        if depth > self.max_container_depth:
            raise ParameterSchemaSourceRejected(ParameterSchemaSourceFailure.CAPACITY)
        self.budget.reserve(depth, entries=_FRAME_ENTRIES)
        if any(frame.source is source for frame in self.frames):
            raise ParameterSchemaSourceRejected(ParameterSchemaSourceFailure.INVALID)
        if isinstance(source, dict):
            rows = self._rows(source)
            self.budget.reserve(entries=_PAIR_ENTRIES * len(rows))
            self.frames.append(_ObjectFrame(source, rows))
        else:
            count = len(source)
            self.budget.reserve(entries=_LIST_ENTRIES * count)
            self.frames.append(_ArrayFrame(source, count))

    def _append(self, node: SchemaNode) -> int:
        self.budget.reserve(entries=_LIST_ENTRIES)
        index = len(self.nodes)
        self.nodes.append(node)
        return index

    def _scalar(self, value: object) -> int:
        return self._append(
            prepare_parameter_schema_scalar(
                value, max_integer_bits=self.max_integer_bits, budget=self.budget
            )
        )

    def _object_node(self, members: list[tuple[int, int]], maximum_key: int) -> int:
        count = len(members)
        self.budget.reserve(1 + count, entries=1)
        return self._append(
            SchemaNode(
                "object",
                members=tuple(members),
                size=2 + count + max(0, count - 1),
                maximum_key=maximum_key,
            )
        )

    def _seal(self, frame: _Frame) -> int:
        if isinstance(frame, _ObjectFrame):
            return self._object_node(frame.members, frame.maximum_key)
        count = len(frame.children)
        self.budget.reserve(1 + count, entries=1)
        return self._append(
            SchemaNode(
                "array", children=tuple(frame.children), size=2 + max(0, count - 1)
            )
        )

    def _next(self, frame: _Frame) -> object:
        self.budget.reserve()
        offset = frame.offset
        frame.offset += 1
        if isinstance(frame, _ArrayFrame):
            if len(frame.source) != frame.count:
                raise ParameterSchemaSourceRejected(
                    ParameterSchemaSourceFailure.INVALID
                )
            return frame.source[offset]
        name, value = frame.rows[offset]
        frame.key = self._scalar(name)
        frame.maximum_key = max(frame.maximum_key, len(name))
        return value

    def _attach(self, frame: _Frame, child: int) -> None:
        self.budget.reserve()
        if isinstance(frame, _ArrayFrame):
            frame.children.append(child)
        else:
            key = frame.key
            if key is None:
                raise ParameterSchemaSourceRejected(
                    ParameterSchemaSourceFailure.INVALID
                )
            frame.members.append((key, child))
            frame.key = None

    def document(self, root: dict[object, object]) -> int:
        self._open(root)
        while self.frames:
            self.budget.reserve()
            frame = self.frames[-1]
            count = len(frame.rows) if isinstance(frame, _ObjectFrame) else frame.count
            if frame.offset == count:
                node = self._seal(frame)
                self.frames.pop()
                if not self.frames:
                    return node
                self._attach(self.frames[-1], node)
                continue
            value = self._next(frame)
            if type(value) is dict or type(value) is list:
                self._open(cast("dict[object, object] | list[object]", value))
            else:
                self._attach(frame, self._scalar(value))
        raise ParameterSchemaSourceRejected(ParameterSchemaSourceFailure.INVALID)

    def named_table(self, source: dict[object, object]) -> int:
        rows = self._rows(source, named_roots=True)
        self.budget.reserve(entries=_PAIR_ENTRIES * len(rows))
        members: list[tuple[int, int]] = []
        maximum_key = 0
        for index in range(len(rows)):
            self.budget.reserve()
            name, root = rows[index]
            key = self._scalar(name)
            document = self.document(cast("dict[object, object]", root))
            self.budget.reserve()
            members.append((key, document))
            maximum_key = max(maximum_key, len(name))
        return self._object_node(members, maximum_key)

    def finish(self, root: int, type_roots: int) -> ParameterSchemaArena:
        self.budget.reserve(1 + len(self.nodes), entries=1)
        return ParameterSchemaArena(tuple(self.nodes), root, type_roots)


def _build(
    parameters: object,
    type_schemas: object,
    budget: ParameterReferenceBudget,
    max_container_depth: int,
    max_integer_bits: int,
) -> ParameterSchemaArena:
    if (
        type(parameters) is not dict
        or type(type_schemas) is not dict
        or not valid_parameter_reference_budget(budget)
        or any(
            type(limit) is not int or not 0 <= limit <= sys.maxsize
            for limit in (max_container_depth, max_integer_bits)
        )
    ):
        raise ParameterSchemaSourceRejected(ParameterSchemaSourceFailure.INVALID)
    builder = _Builder(budget, max_container_depth, max_integer_bits)
    root = builder.document(cast("dict[object, object]", parameters))
    type_roots = builder.named_table(cast("dict[object, object]", type_schemas))
    return builder.finish(root, type_roots)


def build_parameter_schema_arena(
    parameters: object,
    type_schemas: object,
    *,
    budget: ParameterReferenceBudget,
    max_container_depth: int,
    max_integer_bits: int,
) -> ParameterSchemaArena:
    try:
        return _build(
            parameters, type_schemas, budget, max_container_depth, max_integer_bits
        )
    except ParameterSchemaSourceRejected as rejected:
        reason = rejected.reason
    except ParameterScalarRejected as rejected:
        reason = (
            ParameterSchemaSourceFailure.CAPACITY
            if rejected.reason is ParameterScalarFailure.CAPACITY
            else ParameterSchemaSourceFailure.INVALID
        )
        rejected.__traceback__ = None
    except ParameterReferenceRejected as rejected:
        reason = (
            ParameterSchemaSourceFailure.CAPACITY
            if rejected.reason is ParameterReferenceFailure.CAPACITY
            else ParameterSchemaSourceFailure.INVALID
        )
    except MemoryError:
        reason = ParameterSchemaSourceFailure.CAPACITY
    del parameters, type_schemas, budget, max_container_depth, max_integer_bits
    raise_safe(ParameterSchemaSourceRejected(reason))
