import sys
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum, StrEnum, auto

from kora.cel_eval.errors import raise_safe
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceKeyword,
    ParameterReferenceRejected,
    ParameterSchemaArena,
    ParameterSchemaPosition,
    is_schema_node,
    resolve_parameter_reference,
    schema_member,
    schema_node,
    valid_parameter_reference_budget,
)
from kora.ir.structured_schema_input import SchemaNode

_EXCLUDED = (
    "pattern",
    "patternProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_FRAME_ENTRIES: int = 12


class ParameterSupportFailure(StrEnum):
    INVALID = "invalid pinned parameter schema support input"
    CAPACITY = "pinned parameter schema support capacity exceeded"
    EXCLUDED = "pinned parameter schema uses an excluded assertion"
    CYCLE = "pinned parameter schema has a reachable call cycle"


class ParameterSupportRejected(ValueError):
    def __init__(self, reason: ParameterSupportFailure) -> None:
        self.reason: ParameterSupportFailure = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class ParameterSchemaSupport:
    reachable_positions: int
    call_depth: int


class _Group(Enum):
    SINGLE = auto()
    ARRAY = auto()
    MAP = auto()
    CONDITIONAL = auto()


_HEADS: tuple[tuple[str, _Group | ParameterReferenceKeyword], ...] = (
    ("$dynamicRef", ParameterReferenceKeyword.DYNAMIC_REF),
    ("$ref", ParameterReferenceKeyword.REF),
    ("additionalProperties", _Group.SINGLE),
    ("allOf", _Group.ARRAY),
    ("anyOf", _Group.ARRAY),
    ("contains", _Group.SINGLE),
    ("dependentSchemas", _Group.MAP),
    ("if", _Group.CONDITIONAL),
    ("prefixItems", _Group.ARRAY),
    ("items", _Group.SINGLE),
    ("not", _Group.SINGLE),
    ("oneOf", _Group.ARRAY),
    ("properties", _Group.MAP),
    ("propertyNames", _Group.SINGLE),
)
_Edges = Generator[ParameterSchemaPosition, None, None]


@dataclass(slots=True)
class _State:
    document_root: int
    suffix: int | None = None


@dataclass(slots=True, repr=False)
class _Frame:
    state: _State
    edges: _Edges
    height: int = 1


class _Traversal:
    def __init__(
        self,
        arena: ParameterSchemaArena,
        budget: ParameterReferenceBudget,
        max_frames: int,
        max_depth: int,
    ) -> None:
        self.arena: ParameterSchemaArena = arena
        self.budget: ParameterReferenceBudget = budget
        self.max_frames: int = max_frames
        self.max_depth: int = max_depth
        count = len(arena.nodes)
        budget.reserve(1 + count, entries=count)
        empty: list[_State | None] = [None]
        self.states: list[_State | None] = empty * count
        self.frames: list[_Frame] = []
        self.reached: int = 0

    def _member(self, node: SchemaNode, key: str) -> int | None:
        return schema_member(self.arena.nodes, node, key, self.budget)

    def _child(self, document: int, node: int) -> ParameterSchemaPosition:
        self.budget.reserve(entries=1)
        return ParameterSchemaPosition(document, node)

    def _conditional(self, document: int, node: SchemaNode, condition: int) -> _Edges:
        then = self._member(node, "then")
        otherwise = None
        if then is None:
            otherwise = self._member(node, "else")
            if otherwise is None:
                return
        yield self._child(document, condition)
        if then is not None:
            yield self._child(document, then)
            otherwise = self._member(node, "else")
        if otherwise is not None:
            yield self._child(document, otherwise)

    def _group(self, document: int, node_id: int, group: _Group) -> _Edges:
        node = schema_node(self.arena.nodes, node_id, self.budget)
        if group is _Group.ARRAY and node.kind == "array":
            for index in range(len(node.children)):
                self.budget.reserve()
                yield self._child(document, node.children[index])
        elif group is _Group.MAP and node.kind == "object":
            for index in range(len(node.members)):
                self.budget.reserve()
                yield self._child(document, node.members[index][1])
        else:
            raise ParameterSupportRejected(ParameterSupportFailure.INVALID)

    def _edges(self, position: ParameterSchemaPosition) -> _Edges:
        document, index = position.document_root, position.node
        node = schema_node(self.arena.nodes, index, self.budget)
        if not is_schema_node(node):
            raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
        if node.kind != "object":
            return
        if index != document and self._member(node, "$id") is not None:
            raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
        for keyword in _EXCLUDED:
            self.budget.reserve()
            if self._member(node, keyword) is not None:
                raise ParameterSupportRejected(ParameterSupportFailure.EXCLUDED)
        for keyword, group in _HEADS:
            self.budget.reserve()
            member = self._member(node, keyword)
            if member is None:
                continue
            if isinstance(group, ParameterReferenceKeyword):
                self.budget.reserve(entries=1)
                yield resolve_parameter_reference(
                    self.arena,
                    position,
                    keyword=group,
                    budget=self.budget,
                )
            elif group is _Group.SINGLE:
                yield self._child(document, member)
            elif group is _Group.CONDITIONAL:
                yield from self._conditional(document, node, member)
            else:
                yield from self._group(document, member, group)

    def _push(self, position: ParameterSchemaPosition) -> None:
        self.budget.reserve()
        if len(self.frames) >= self.max_frames:
            raise ParameterSupportRejected(ParameterSupportFailure.CAPACITY)
        self.budget.reserve(entries=_FRAME_ENTRIES)
        state = _State(position.document_root)
        self.frames.append(_Frame(state, self._edges(position)))
        self.states[position.node] = state
        self.reached += 1

    def _propagate(self, parent: _Frame, suffix: int) -> None:
        self.budget.reserve()
        height = 1 + suffix
        if len(self.frames) + suffix > self.max_depth:
            raise ParameterSupportRejected(ParameterSupportFailure.CAPACITY)
        parent.height = max(parent.height, height)

    def _follow(self, frame: _Frame, target: ParameterSchemaPosition) -> None:
        self.budget.reserve()
        index = target.node
        if type(index) is not int or not 0 <= index < len(self.states):
            raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
        state = self.states[index]
        if state is None:
            self._push(target)
        elif state.document_root != target.document_root:
            raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
        elif state.suffix is None:
            raise ParameterSupportRejected(ParameterSupportFailure.CYCLE)
        else:
            self._propagate(frame, state.suffix)

    def walk(self, root: int) -> ParameterSchemaSupport:
        try:
            self.budget.reserve(entries=1)
            self._push(ParameterSchemaPosition(root, root))
            while self.frames:
                self.budget.reserve()
                frame = self.frames[-1]
                target = next(frame.edges, None)
                if target is not None:
                    self._follow(frame, target)
                    continue
                self.budget.reserve()
                if len(self.frames) - 1 + frame.height > self.max_depth:
                    raise ParameterSupportRejected(ParameterSupportFailure.CAPACITY)
                frame.state.suffix = frame.height
                self.frames.pop()
                if self.frames:
                    self._propagate(self.frames[-1], frame.height)
                else:
                    return ParameterSchemaSupport(self.reached, frame.height)
        finally:
            while self.frames:
                self.frames.pop().edges.close()
        raise ParameterSupportRejected(ParameterSupportFailure.INVALID)


def _check(
    arena: ParameterSchemaArena,
    budget: ParameterReferenceBudget,
    max_active_frames: int,
    max_call_depth: int,
) -> ParameterSchemaSupport:
    if (
        type(arena) is not ParameterSchemaArena
        or not valid_parameter_reference_budget(budget)
        or any(
            type(limit) is not int or not 0 <= limit <= sys.maxsize
            for limit in (max_active_frames, max_call_depth)
        )
    ):
        raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
    root = schema_node(arena.nodes, arena.root, budget)
    roots = schema_node(arena.nodes, arena.type_roots, budget)
    if root.kind != "object" or roots.kind != "object":
        raise ParameterSupportRejected(ParameterSupportFailure.INVALID)
    return _Traversal(arena, budget, max_active_frames, max_call_depth).walk(arena.root)


def check_parameter_schema_support(
    arena: ParameterSchemaArena,
    *,
    budget: ParameterReferenceBudget,
    max_active_frames: int,
    max_call_depth: int,
) -> ParameterSchemaSupport:
    try:
        return _check(arena, budget, max_active_frames, max_call_depth)
    except ParameterSupportRejected as rejected:
        reason = rejected.reason
    except ParameterReferenceRejected as rejected:
        reason = (
            ParameterSupportFailure.CAPACITY
            if rejected.reason is ParameterReferenceFailure.CAPACITY
            else ParameterSupportFailure.INVALID
        )
        rejected.__traceback__ = None
    except MemoryError:
        reason = ParameterSupportFailure.CAPACITY
    del arena, budget, max_active_frames, max_call_depth
    raise_safe(ParameterSupportRejected(reason))
