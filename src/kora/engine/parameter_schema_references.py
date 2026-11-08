import sys
from dataclasses import dataclass
from enum import StrEnum

from kora.cel_eval.errors import raise_safe
from kora.ir.schema_locations import Position, schema_child_position
from kora.ir.structured_schema_input import FrozenSchemaArena, SchemaNode
from kora.ir.structured_schema_locations import decode_reference
from kora.ir.structured_schema_work import PreparationRefused


class ParameterReferenceKeyword(StrEnum):
    REF = "$ref"
    DYNAMIC_REF = "$dynamicRef"


class ParameterReferenceFailure(StrEnum):
    INVALID = "invalid pinned parameter schema reference"
    CAPACITY = "pinned parameter reference capacity exceeded"


class ParameterReferenceRejected(ValueError):
    def __init__(self, reason: ParameterReferenceFailure) -> None:
        self.reason: ParameterReferenceFailure = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class ParameterSchemaPosition:
    document_root: int
    node: int


@dataclass(slots=True)
class ParameterReferenceBudget:
    work: int
    scratch_bytes: int
    scratch_entries: int

    def reserve(self, work: int = 1, byte_count: int = 0, entries: int = 0) -> None:
        if any(
            type(charge) is not int or charge < 0
            for charge in (work, byte_count, entries)
        ):
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
        if (
            work > self.work
            or byte_count > self.scratch_bytes
            or entries > self.scratch_entries
        ):
            raise ParameterReferenceRejected(ParameterReferenceFailure.CAPACITY)
        self.work -= work
        self.scratch_bytes -= byte_count
        self.scratch_entries -= entries


def _node(
    nodes: tuple[SchemaNode, ...], index: int, budget: ParameterReferenceBudget
) -> SchemaNode:
    budget.reserve()
    if type(index) is not int or not 0 <= index < len(nodes):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    return nodes[index]


def _name(
    nodes: tuple[SchemaNode, ...], index: int, budget: ParameterReferenceBudget
) -> str:
    value = _node(nodes, index, budget).value
    if type(value) is not str:
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    return value


def _member(
    nodes: tuple[SchemaNode, ...],
    current: SchemaNode,
    key: str,
    budget: ParameterReferenceBudget,
) -> int | None:
    budget.reserve()
    if current.kind != "object":
        return None
    members = current.members
    low, high = 0, len(members)
    while low < high:
        budget.reserve()
        middle = (low + high) // 2
        candidate = _name(nodes, members[middle][0], budget)
        budget.reserve(1 + min(len(key), len(candidate)))
        if candidate < key:
            low = middle + 1
        else:
            high = middle
    if low == len(members):
        return None
    budget.reserve()
    key_node, child = members[low]
    candidate = _name(nodes, key_node, budget)
    budget.reserve(1 + min(len(key), len(candidate)))
    return child if candidate == key else None


def _array_child(node: SchemaNode, key: str, budget: ParameterReferenceBudget) -> int:
    budget.reserve()
    count = len(node.children)
    width = len(key)
    if not width or width > len(str(count)):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    budget.reserve(1 + width * width)
    if not key.isascii() or not key.isdigit() or (width > 1 and key.startswith("0")):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    index = int(key)
    if index >= count:
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    return node.children[index]


def _is_schema(node: SchemaNode) -> bool:
    return node.kind == "object" or type(node.value) is bool


def _matches_position(position: Position, node: SchemaNode) -> bool:
    if position is Position.SCHEMA:
        return _is_schema(node)
    if position is Position.LISTED:
        return node.kind == "array"
    return position is not Position.DATA and node.kind == "object"


def _descend(
    nodes: tuple[SchemaNode, ...],
    root: int,
    pool: list[str],
    used: int,
    budget: ParameterReferenceBudget,
) -> ParameterSchemaPosition:
    index = root
    current = _node(nodes, index, budget)
    if not _is_schema(current):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    position = Position.SCHEMA
    for offset in range(used):
        budget.reserve()
        key = pool[offset]
        if current.kind == "object":
            child = _member(nodes, current, key, budget)
            if child is None:
                raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
            index = child
        elif current.kind == "array":
            index = _array_child(current, key, budget)
        else:
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
        current = _node(nodes, index, budget)
        budget.reserve(1 + len(key))
        position = schema_child_position(position, key, _is_schema(current))
        if not _matches_position(position, current) or (
            position is Position.SCHEMA
            and _member(nodes, current, "$id", budget) is not None
        ):
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    budget.reserve()
    if position is not Position.SCHEMA or not _is_schema(current):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    return ParameterSchemaPosition(root, index)


def _resolve(
    arena: FrozenSchemaArena,
    origin: ParameterSchemaPosition,
    type_roots: int,
    keyword: ParameterReferenceKeyword,
    budget: ParameterReferenceBudget,
) -> ParameterSchemaPosition:
    if (
        type(arena) is not FrozenSchemaArena
        or type(origin) is not ParameterSchemaPosition
        or type(keyword) is not ParameterReferenceKeyword
        or type(budget) is not ParameterReferenceBudget
        or any(
            type(limit) is not int or not 0 <= limit <= sys.maxsize
            for limit in (budget.work, budget.scratch_bytes, budget.scratch_entries)
        )
    ):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    nodes = arena.nodes
    root = origin.document_root
    if not _is_schema(_node(nodes, root, budget)):
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    source = _node(nodes, origin.node, budget)
    member = _member(nodes, source, keyword.value, budget)
    if member is None:
        raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
    reference = _name(nodes, member, budget)
    length = len(reference) + 1
    budget.reserve(length, 20 * length, 8 * length)
    name, pool, used = decode_reference(reference, budget)
    if name is not None:
        named = _node(nodes, type_roots, budget)
        selected = _member(nodes, named, name, budget)
        if selected is None:
            raise ParameterReferenceRejected(ParameterReferenceFailure.INVALID)
        root = selected
    return _descend(nodes, root, pool, used, budget)


def resolve_parameter_reference(
    arena: FrozenSchemaArena,
    origin: ParameterSchemaPosition,
    *,
    type_roots: int,
    keyword: ParameterReferenceKeyword,
    budget: ParameterReferenceBudget,
) -> ParameterSchemaPosition:
    try:
        return _resolve(arena, origin, type_roots, keyword, budget)
    except ParameterReferenceRejected as rejected:
        reason = rejected.reason
    except PreparationRefused:
        reason = ParameterReferenceFailure.INVALID
    except MemoryError:
        reason = ParameterReferenceFailure.CAPACITY
    del arena, origin, type_roots, keyword, budget
    raise_safe(ParameterReferenceRejected(reason))
