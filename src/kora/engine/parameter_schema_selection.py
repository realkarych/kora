from dataclasses import dataclass
from typing import cast

from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceKeyword,
    ParameterReferenceRejected,
    ParameterSchemaArena,
    ParameterSchemaPosition,
    decode_parameter_reference,
    is_schema_node,
    resolve_parameter_reference,
)
from kora.ir.schema_locations import Position, schema_child_position
from kora.ir.schema_validation import REFERENCE_KEYWORDS
from kora.ir.structured_schema_input import SchemaNode

_FRAME_ENTRIES = 8
_LIST_ENTRIES = 5


@dataclass(slots=True)
class _Frame:
    node: int
    position: Position
    offset: int = 0


@dataclass(slots=True, repr=False)
class ParameterSchemaSelection:
    records: list[object]
    references: list[tuple[int, ParameterReferenceKeyword]]

    def verify(
        self, arena: ParameterSchemaArena, budget: ParameterReferenceBudget
    ) -> None:
        for node, keyword in self.references:
            budget.reserve(2, entries=2)
            resolve_parameter_reference(
                arena,
                ParameterSchemaPosition(arena.root, node),
                keyword=keyword,
                budget=budget,
            )


def _typed_child(position: Position, node: SchemaNode) -> bool:
    if position is Position.SCHEMA:
        return is_schema_node(node)
    if position is Position.LISTED:
        return node.kind == "array"
    return position is not Position.DATA and node.kind == "object"


class _Selection:
    def __init__(self, records: list[object], budget: ParameterReferenceBudget) -> None:
        self.records: list[object] = records
        self.budget: ParameterReferenceBudget = budget
        self.marks: bytearray | None = None
        self.frontier: list[int] = []
        self.references: list[tuple[int, ParameterReferenceKeyword]] = []

    def _record(self, index: int) -> dict[str, object]:
        row = self.records[index]
        if type(row) is not dict:
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
        return cast("dict[str, object]", row)

    def _enqueue(self, index: int) -> None:
        budget = self.budget
        budget.reserve(2)
        marks = self.marks
        if marks is None:
            count = len(self.records)
            budget.reserve(1 + count, byte_count=count, entries=1)
            marks = self.marks = bytearray(count)
        if not marks[index]:
            budget.reserve(2, entries=_LIST_ENTRIES)
            marks[index] = 1
            self.frontier.append(index)

    def _select(self, name: str) -> None:
        budget = self.budget
        budget.reserve(2)
        low, high = 0, len(self.records)
        while low < high:
            budget.reserve(5)
            middle = (low + high) // 2
            candidate = self._record(middle).get("name")
            if type(candidate) is not str:
                raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
            budget.reserve(2 + 2 * min(len(name), len(candidate)))
            if name == candidate:
                self._enqueue(middle)
                return
            if name < candidate:
                high = middle
            else:
                low = middle + 1
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)

    def _reference(self, origin: int, key: str, operand: SchemaNode) -> None:
        budget = self.budget
        budget.reserve(2)
        reference = operand.value
        if type(reference) is not str:
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
        name = decode_parameter_reference(reference, budget)[0]
        if name is not None:
            self._select(name)
        budget.reserve(2, entries=1 + _LIST_ENTRIES)
        self.references.append((origin, ParameterReferenceKeyword(key)))

    def root_references(self, nodes: list[SchemaNode], root: int) -> None:
        budget = self.budget
        budget.reserve(2, entries=_FRAME_ENTRIES)
        frames = [_Frame(root, Position.SCHEMA)]
        while frames:
            budget.reserve(4)
            frame = frames[-1]
            node, position, offset = nodes[frame.node], frame.position, frame.offset
            if position is Position.LISTED:
                if offset == len(node.children):
                    frames.pop()
                    continue
                child = node.children[offset]
                child_position = Position.SCHEMA
            else:
                if offset == len(node.members):
                    frames.pop()
                    continue
                budget.reserve(3)
                key_node, child = node.members[offset]
                key = nodes[key_node].value
                if type(key) is not str:
                    raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
                budget.reserve(2 + len(key))
                if position is Position.SCHEMA:
                    if key == "$id" and frame.node != root:
                        raise ParameterReferenceRejected(
                            ParameterReferenceFailure.INVALID
                        )
                    if key in REFERENCE_KEYWORDS:
                        self._reference(frame.node, key, nodes[child])
                child_position = schema_child_position(
                    position, key, is_schema_node(nodes[child])
                )
            frame.offset += 1
            if _typed_child(child_position, nodes[child]):
                budget.reserve(2, entries=_FRAME_ENTRIES)
                frames.append(_Frame(child, child_position))

    def named_closure(self) -> ParameterSchemaSelection:
        budget = self.budget
        offset = 0
        while True:
            budget.reserve(2)
            if offset == len(self.frontier):
                break
            row = self._record(self.frontier[offset])
            offset += 1
            references = row.get("references")
            if type(references) is not list:
                raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
            for reference in cast("list[object]", references):
                budget.reserve(3)
                if type(reference) is not dict:
                    raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
                name = cast("dict[str, object]", reference).get("target_type")
                if type(name) is not str:
                    raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
                self._select(name)
        marks = self.marks
        budget.reserve(2, entries=2)
        selected: list[object] = []
        if marks is not None:
            budget.reserve(1 + len(marks), entries=_LIST_ENTRIES * len(self.frontier))
            for index, marked in enumerate(marks):
                if marked:
                    selected.append(self.records[index])
        return ParameterSchemaSelection(selected, self.references)


def select_parameter_schema_documents(
    nodes: list[SchemaNode],
    root: int,
    records: list[object],
    budget: ParameterReferenceBudget,
) -> ParameterSchemaSelection:
    budget.reserve(2, entries=3)
    selection = _Selection(records, budget)
    selection.root_references(nodes, root)
    return selection.named_closure()
