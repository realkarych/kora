import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import cached_property
from types import MappingProxyType
from typing import Final, cast

from kora.cel.diagnostics import Reason, fail
from kora.cel.functions import Charge, Form, lookup
from kora.cel.meter import Meter
from kora.cel.regex import parse_pattern
from kora.cel.tree import (
    Binary,
    BinaryOperator,
    BoolLiteral,
    BytesLiteral,
    Call,
    Comprehension,
    Conditional,
    DoubleLiteral,
    Has,
    Identifier,
    Index,
    IntLiteral,
    ListLiteral,
    Macro,
    MapLiteral,
    Node,
    NullLiteral,
    Select,
    StringLiteral,
    UintLiteral,
    Unary,
    UnaryOperator,
)
from kora.cel.types import (
    BOOL,
    BYTES,
    DOUBLE,
    DYN,
    INT,
    NULL,
    STRING,
    UINT,
    CelType,
    Dyn,
    Kind,
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Record,
    RecordField,
    element_type,
    is_concatenable,
    iteration_type,
    member_type,
    unwrap,
)

DATA_BYTES: Final[int] = 4096
DATA_VALUES: Final[int] = 256
DATA_ITEMS: Final[int] = 1000
DATA_STRING_BYTES: Final[int] = 16384
DATA_DEPTH: Final[int] = 32
SCALAR_BYTES: Final[int] = 24
BOUND_NODE_BYTES: Final[int] = 1024
BOUND_FIELD_BYTES: Final[int] = 128
BOUND_VARIABLE_BYTES: Final[int] = 256
BOUND_NAME_CHARACTER_BYTES: Final[int] = 8
COMPARISON_OPERATORS: Final[frozenset[BinaryOperator]] = frozenset(
    {
        BinaryOperator.EQUAL,
        BinaryOperator.NOT_EQUAL,
        BinaryOperator.LESS,
        BinaryOperator.LESS_EQUAL,
        BinaryOperator.GREATER,
        BinaryOperator.GREATER_EQUAL,
        BinaryOperator.IN,
    }
)
LOGICAL_OPERATORS: Final[frozenset[BinaryOperator]] = frozenset(
    {BinaryOperator.AND, BinaryOperator.OR}
)


@dataclass(frozen=True)
class Weights:
    evaluation: int
    convert_byte: int
    convert_value: int
    node: int
    iteration: int
    map_quadratic: int
    concat_byte: int
    concat_value: int
    compare_byte: int
    compare_value: int
    function_byte: int
    pattern_compile: int
    pattern_match: int
    rendering_character: int
    memory_base: int
    memory_byte: int


@dataclass(frozen=True)
class Budget:
    steps: int
    memory: int


PROVISIONAL_WEIGHTS: Final[Weights] = Weights(
    evaluation=1_600_000_000,
    convert_byte=80_000,
    convert_value=50_000_000,
    node=30_000_000,
    iteration=100_000_000,
    map_quadratic=14_000_000,
    concat_byte=100_000,
    concat_value=50_000_000,
    compare_byte=100_000,
    compare_value=30_000_000,
    function_byte=200_000,
    pattern_compile=2_400_000,
    pattern_match=40,
    rendering_character=1_000_000_000,
    memory_base=0,
    memory_byte=1,
)

PROVISIONAL_BUDGET: Final[Budget] = Budget(
    steps=100_000_000_000_000, memory=16 * 1024 * 1024
)


@dataclass(frozen=True)
class ValueBound:
    bytes: int
    values: int
    items: int
    string_bytes: int
    element: "ValueBound | None" = None
    fields: Mapping[str, "ValueBound"] = field(default_factory=lambda: {})


DATA_BOUND: Final[ValueBound] = ValueBound(
    bytes=DATA_BYTES,
    values=DATA_VALUES,
    items=DATA_ITEMS,
    string_bytes=DATA_STRING_BYTES,
)


@dataclass(frozen=True)
class Variable:
    type: CelType
    bound: ValueBound = DATA_BOUND


class Quantity(Enum):
    BYTES = "bytes"
    VALUES = "values"


Key = tuple[int, Quantity]


@dataclass(frozen=True)
class Affine:
    constant: int
    terms: tuple[tuple[Key, int], ...] = ()

    @staticmethod
    def of(value: int) -> "Affine":
        return Affine(value)

    @staticmethod
    def term(key: Key) -> "Affine":
        return Affine(0, ((key, 1),))

    def coefficients(self) -> dict[Key, int]:
        return dict(self.terms)

    def plus(self, other: "Affine") -> "Affine":
        merged = self.coefficients()
        for key, coefficient in other.terms:
            merged[key] = merged.get(key, 0) + coefficient
        return Affine(
            self.constant + other.constant, tuple(sorted(merged.items(), key=sort_key))
        )

    def times(self, factor: int) -> "Affine":
        return Affine(
            self.constant * factor,
            tuple((key, coefficient * factor) for key, coefficient in self.terms),
        )

    def join(self, other: "Affine") -> "Affine":
        merged = self.coefficients()
        for key, coefficient in other.terms:
            merged[key] = max(merged.get(key, 0), coefficient)
        return Affine(
            max(self.constant, other.constant),
            tuple(sorted(merged.items(), key=sort_key)),
        )

    def substitute(self, key: Key, replacement: "Affine") -> "Affine":
        coefficients = self.coefficients()
        coefficient = coefficients.pop(key, 0)
        rest = Affine(self.constant, tuple(sorted(coefficients.items(), key=sort_key)))
        return rest.plus(replacement.times(coefficient))

    def concrete(self) -> int:
        if self.terms:
            raise ValueError("affine bound still depends on a comprehension variable")
        return self.constant


def sort_key(item: tuple[Key, int]) -> tuple[int, str]:
    (scope, quantity), _ = item
    return scope, quantity.value


ZERO: Final[Affine] = Affine(0)


def sum_affine(values: list[Affine]) -> Affine:
    result = ZERO
    for value in values:
        result = result.plus(value)
    return result


@dataclass(frozen=True)
class Shape:
    type: CelType
    bytes: Affine
    values: Affine
    items: int
    string_bytes: int
    element: "Shape | None" = None
    fields: Mapping[str, "Shape"] = field(default_factory=lambda: {})

    @cached_property
    def free(self) -> bool:
        return (
            not self.bytes.terms
            and not self.values.terms
            and (self.element is None or self.element.free)
            and all(child.free for child in self.fields.values())
        )

    def hereditary(self, child_type: CelType) -> "Shape":
        return Shape(child_type, self.bytes, self.values, DATA_ITEMS, DATA_STRING_BYTES)

    def member(self) -> "Shape":
        if self.element is not None and not self.fields:
            return self.element
        return self.hereditary(element_type(self.type))

    def indexed(self, child_type: CelType, name: str | None) -> "Shape":
        if name is not None and name in self.fields:
            return self.child(child_type, name)
        if self.fields:
            return self.hereditary(child_type)
        return self.child(child_type, None)

    def child(self, child_type: CelType, name: str | None) -> "Shape":
        declared = self.fields.get(name) if name is not None else None
        if declared is None:
            declared = self.element
        if declared is None:
            return self.hereditary(child_type)
        if isinstance(child_type, Dyn) or declared.type is child_type:
            return declared
        return replace(declared, type=child_type)

    def substituted(self, key: Key, replacement: Affine) -> "Shape":
        if self.free:
            return self
        element = (
            self.element.substituted(key, replacement)
            if self.element is not None
            else None
        )
        fields = {
            name: child.substituted(key, replacement)
            for name, child in self.fields.items()
        }
        return Shape(
            self.type,
            self.bytes.substitute(key, replacement),
            self.values.substitute(key, replacement),
            self.items,
            self.string_bytes,
            element,
            fields,
        )

    def key(self) -> "Shape":
        return Shape(STRING, self.bytes, Affine.of(1), 0, DATA_STRING_BYTES)

    def iterated(self) -> "Shape":
        inner = unwrap(self.type)
        if isinstance(inner, ListOf):
            return self.child(inner.element, None)
        if isinstance(inner, MapOf | Record):
            return replace(self.key(), type=iteration_type(self.type))
        return join_shapes(self.member(), self.key())


def scalar(cel_type: CelType, size: int = SCALAR_BYTES) -> Shape:
    return Shape(cel_type, Affine.of(size), Affine.of(1), 0, 0)


class BoundError(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    bytes: int
    values: int
    items: int
    string_bytes: int
    element: ValueBound | None
    fields: tuple[tuple[str, ValueBound], ...]

    def children(self) -> list[ValueBound]:
        element = [self.element] if self.element is not None else []
        return [*element, *(child for _, child in self.fields)]


def is_integer(value: object) -> bool:
    return type(value) is int


def is_exact_bound(value: object) -> bool:
    return type(value) is ValueBound


def is_plain_mapping(value: object) -> bool:
    return type(value) is dict


def check_environment(environment: Mapping[str, Variable]) -> None:
    if not is_plain_mapping(environment):
        raise BoundError("the environment is not a plain dict")
    if not all(map(is_text, environment)):
        raise BoundError("an environment name is not a string")


def check_type(root: object, meter: Meter | None = None) -> None:
    seen: set[int] = set()
    pending: list[object] = [root]
    while pending:
        cel_type = pending.pop()
        if id(cel_type) in seen:
            continue
        seen.add(id(cel_type))
        children = type_children(cel_type)
        if meter is not None:
            meter.tick(1 + len(children) + allowed_size(cel_type))
        pending.extend(children)


def allowed_size(cel_type: object) -> int:
    if isinstance(cel_type, Primitive) and cel_type.allowed is not None:
        return len(cel_type.allowed)
    return 0


def type_children(cel_type: object) -> list[object]:
    kind = type(cel_type)
    if kind is Primitive and isinstance(cel_type, Primitive):
        if type(cel_type.kind) is not Kind or not is_allowed_set(cel_type.allowed):
            raise BoundError("a variable type is not a CEL type")
        return []
    if kind is Dyn:
        return []
    if kind is ListOf and isinstance(cel_type, ListOf):
        return [cel_type.element]
    if kind is MapOf and isinstance(cel_type, MapOf):
        return [cel_type.key, cel_type.value]
    if kind is Nullable and isinstance(cel_type, Nullable):
        if type(cel_type.inner) is Nullable:
            raise BoundError("a nullable type wraps another nullable type")
        return [cel_type.inner]
    if kind is Record and isinstance(cel_type, Record):
        return record_children(cel_type)
    raise BoundError("a variable type is not a CEL type")


def record_children(record: Record) -> list[object]:
    fields = record.fields
    if not is_plain_mapping(fields):
        raise BoundError("a variable type is not a CEL type")
    children: list[object] = []
    for name, declared in fields.items():
        if not is_text(name) or not is_record_field(declared):
            raise BoundError("a variable type is not a CEL type")
        if type(declared.required) is not bool:
            raise BoundError("a variable type is not a CEL type")
        children.append(declared.type)
    if record.extra is not None:
        children.append(record.extra)
    return children


def is_allowed_set(allowed: object) -> bool:
    if allowed is None:
        return True
    return type(allowed) is frozenset and all(
        map(is_text, cast("frozenset[object]", allowed))
    )


def is_record_field(value: object) -> bool:
    return type(value) is RecordField


def is_variable(value: object) -> bool:
    return type(value) is Variable


def is_text(value: object) -> bool:
    return type(value) is str


def take_snapshot(bound: object) -> Snapshot:
    if not isinstance(bound, ValueBound) or not is_exact_bound(bound):
        raise BoundError("a bound is not a value bound")
    sizes = (bound.bytes, bound.values, bound.items, bound.string_bytes)
    if not all(map(is_integer, sizes)):
        raise BoundError("a value bound size is not an integer")
    if any(size < 0 for size in sizes):
        raise BoundError("a value bound is negative")
    fields = bound.fields
    element = bound.element
    if not is_plain_mapping(fields):
        raise BoundError("a bound's fields are not a plain dict")
    pairs: list[tuple[str, ValueBound]] = []
    for name, child in fields.items():
        if not is_text(name) or not is_exact_bound(child):
            raise BoundError("a bound is not a value bound")
        pairs.append((name, child))
    if element is not None and not is_exact_bound(element):
        raise BoundError("a bound is not a value bound")
    bytes_size, values, items, string_bytes = sizes
    return Snapshot(bytes_size, values, items, string_bytes, element, tuple(pairs))


@dataclass(frozen=True)
class Converted:
    bound: ValueBound
    shape: Shape
    height: int


class BoundShapes:
    def __init__(self, meter: Meter) -> None:
        self.meter: Meter = meter
        self.memo: dict[int, Converted] = {}
        self.snapshots: dict[int, tuple[ValueBound, Snapshot]] = {}

    def shape(self, cel_type: CelType, bound: ValueBound) -> Shape:
        self.convert(bound)
        shape = self.memo[id(bound)].shape
        return replace(shape, type=cel_type)

    def snapshot(self, bound: ValueBound) -> Snapshot:
        known = self.snapshots.get(id(bound))
        if known is not None:
            return known[1]
        taken = take_snapshot(bound)
        self.meter.tick(1 + len(taken.fields))
        self.snapshots[id(bound)] = (bound, taken)
        return taken

    def convert(self, root: ValueBound) -> None:
        if id(root) in self.memo:
            return
        order: dict[int, int] = {}
        low: dict[int, int] = {}
        edges: dict[int, list[ValueBound]] = {}
        component: list[ValueBound] = []
        open_ids: set[int] = set()
        work: list[tuple[ValueBound, int]] = [(root, 0)]
        while work:
            bound, position = work.pop()
            key = id(bound)
            if position == 0:
                order[key] = low[key] = len(order)
                edges[key] = self.snapshot(bound).children()
                component.append(bound)
                open_ids.add(key)
            children = edges[key]
            if position < len(children):
                work.append((bound, position + 1))
                child = children[position]
                child_key = id(child)
                if child_key in self.memo:
                    continue
                if child_key not in order:
                    work.append((child, 0))
                elif child_key in open_ids:
                    low[key] = min(low[key], order[child_key])
                continue
            if low[key] == order[key]:
                self.close_component(bound, component, open_ids, edges)
            if work:
                parent = id(work[-1][0])
                low[parent] = min(low[parent], low[key])

    def close_component(
        self,
        head: ValueBound,
        component: list[ValueBound],
        open_ids: set[int],
        edges: dict[int, list[ValueBound]],
    ) -> None:
        members: list[ValueBound] = []
        while True:
            member = component.pop()
            open_ids.discard(id(member))
            members.append(member)
            if member is head:
                break
        member_ids = {id(member) for member in members}
        cyclic = len(members) > 1 or any(
            id(child) in member_ids for child in edges[id(head)]
        )
        for member in members:
            self.check_children(member)
        for member in members:
            self.finish(member, cyclic)

    def check_children(self, bound: ValueBound) -> None:
        parent = self.snapshot(bound)
        for child in parent.children():
            taken = self.snapshot(child)
            if taken.bytes > parent.bytes or taken.values > parent.values:
                raise BoundError("a child bound is larger than its parent")
            if taken.items > DATA_ITEMS or taken.string_bytes > DATA_STRING_BYTES:
                raise BoundError("a child bound exceeds the data bounds")

    def finish(self, bound: ValueBound, cyclic: bool) -> None:
        taken = self.snapshot(bound)
        element = (
            None if cyclic or taken.element is None else self.memo[id(taken.element)]
        )
        fields = (
            {}
            if cyclic
            else {name: self.memo[id(child)] for name, child in taken.fields}
        )
        heights = [
            part.height for part in (element, *fields.values()) if part is not None
        ]
        height = 1 + max(heights, default=0)
        if height > DATA_DEPTH:
            element = None
            fields = {}
            height = 1
        shape = Shape(
            DYN,
            Affine.of(taken.bytes),
            Affine.of(taken.values),
            taken.items,
            taken.string_bytes,
            element.shape if element is not None else None,
            {name: part.shape for name, part in fields.items()},
        )
        self.memo[id(bound)] = Converted(bound, shape, height)


@dataclass(frozen=True)
class NodeCost:
    shape: Shape
    steps: Affine
    memory: Affine
    allocated: Affine = ZERO


@dataclass(frozen=True)
class Estimate:
    steps: int
    memory: int
    compile_steps: int
    variables: tuple[str, ...]
    bounds: Mapping[str, ValueBound] = field(repr=False)
    bound_bytes: int


def binding_bounds(
    variables: Mapping[str, Shape], meter: Meter
) -> tuple[Mapping[str, ValueBound], int]:
    memo: dict[int, ValueBound] = {}
    charge = BOUND_VARIABLE_BYTES
    for root in variables.values():
        pending = [(root, False)]
        while pending:
            shape, finished = pending.pop()
            if id(shape) in memo:
                continue
            meter.tick()
            if not finished:
                pending.append((shape, True))
                if shape.element is not None:
                    pending.append((shape.element, False))
                pending.extend((child, False) for child in shape.fields.values())
                continue
            fields = {name: memo[id(child)] for name, child in shape.fields.items()}
            charge += BOUND_NODE_BYTES + sum(
                BOUND_FIELD_BYTES + len(name) * BOUND_NAME_CHARACTER_BYTES
                for name in fields
            )
            meter.tick(sum(len(name) for name in fields))
            memo[id(shape)] = ValueBound(
                min(shape.bytes.concrete(), DATA_BYTES),
                min(shape.values.concrete(), DATA_VALUES),
                min(shape.items, DATA_ITEMS),
                min(shape.string_bytes, DATA_STRING_BYTES),
                memo[id(shape.element)] if shape.element is not None else None,
                MappingProxyType(fields),
            )
    charge += sum(
        BOUND_VARIABLE_BYTES + len(name) * BOUND_NAME_CHARACTER_BYTES
        for name in variables
    )
    meter.tick(sum(len(name) for name in variables))
    return MappingProxyType(
        {name: memo[id(root)] for name, root in variables.items()}
    ), charge


@dataclass(frozen=True)
class ScopeVariable:
    scope: int
    shape: Shape


def literal_bytes(node: Node) -> int:
    match node:
        case StringLiteral(value=value):
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        case BytesLiteral(value=value):
            return len(value) + 2
        case IntLiteral(value=value) | UintLiteral(value=value):
            return len(str(value))
        case DoubleLiteral(value=value):
            return len(repr(value))
        case BoolLiteral(value=value):
            return 4 if value else 5
        case _:
            return 4


def literal_type(node: Node) -> CelType:
    match node:
        case StringLiteral():
            return STRING
        case BytesLiteral():
            return BYTES
        case IntLiteral():
            return INT
        case UintLiteral():
            return UINT
        case DoubleLiteral():
            return DOUBLE
        case BoolLiteral():
            return BOOL
        case _:
            return NULL


def held_while(costs: list[NodeCost]) -> tuple[Affine, Affine]:
    held = ZERO
    peak = ZERO
    for cost in costs:
        peak = peak.join(held.plus(cost.memory))
        held = held.plus(cost.allocated)
    return held, peak


class Estimator:
    def __init__(
        self,
        environment: Mapping[str, Variable],
        weights: Weights,
        meter: Meter,
    ) -> None:
        self.meter: Meter = meter
        self.environment: Mapping[str, Variable] = environment
        self.weights: Weights = weights
        self.scopes: dict[str, ScopeVariable] = {}
        self.next_scope: int = 0
        self.variables: dict[str, Shape] = {}
        self.default: Variable = Variable(DYN)
        self.bounds: BoundShapes = BoundShapes(meter)

    def node_steps(self, *parts: Affine) -> Affine:
        return sum_affine([Affine.of(self.weights.node), *parts])

    def allocation(self, size: Affine) -> Affine:
        return size.times(self.weights.memory_byte)

    def visit(self, node: Node) -> NodeCost:
        self.meter.tick()
        match node:
            case StringLiteral() | BytesLiteral():
                size = literal_bytes(node)
                allocated = self.allocation(Affine.of(size))
                shape = scalar(literal_type(node), size)
                return NodeCost(shape, self.node_steps(), allocated, allocated)
            case (
                IntLiteral()
                | UintLiteral()
                | DoubleLiteral()
                | BoolLiteral()
                | NullLiteral()
            ):
                shape = scalar(literal_type(node), literal_bytes(node))
                return NodeCost(shape, self.node_steps(), ZERO)
            case Identifier(name=name):
                return self.identifier(name)
            case Select(operand=operand, name=name):
                inner = self.visit(operand)
                shape = inner.shape.child(member_type(inner.shape.type, name), name)
                steps = self.node_steps(inner.steps)
                return NodeCost(shape, steps, inner.memory, inner.allocated)
            case Index(operand=operand, index=index):
                inner = self.visit(operand)
                key = self.visit(index)
                name = index.value if isinstance(index, StringLiteral) else None
                shape = inner.shape.indexed(element_type(inner.shape.type), name)
                _, peak = held_while([inner, key])
                steps = self.node_steps(inner.steps, key.steps)
                return NodeCost(shape, steps, peak, inner.allocated)
            case Unary(operator=operator, operand=operand):
                inner = self.visit(operand)
                result = BOOL if operator is UnaryOperator.NOT else inner.shape.type
                steps = self.node_steps(inner.steps)
                return NodeCost(scalar(result), steps, inner.memory)
            case Binary():
                return self.binary(node)
            case Conditional(condition=condition, then=then, otherwise=otherwise):
                test = self.visit(condition)
                left = self.visit(then)
                right = self.visit(otherwise)
                shape = join_shapes(left.shape, right.shape)
                steps = self.node_steps(test.steps, left.steps.join(right.steps))
                memory = test.memory.join(left.memory.join(right.memory))
                allocated = left.allocated.join(right.allocated)
                return NodeCost(shape, steps, memory, allocated)
            case ListLiteral(elements=elements):
                return self.list_literal(elements)
            case MapLiteral(entries=entries):
                parts = [part for entry in entries for part in (entry.key, entry.value)]
                return self.map_literal(parts)
            case Has(target=target):
                inner = self.visit(target.operand)
                steps = self.node_steps(inner.steps)
                return NodeCost(scalar(BOOL, 5), steps, inner.memory)
            case Call():
                return self.call(node)
            case Comprehension():
                return self.comprehension(node)

    def identifier(self, name: str) -> NodeCost:
        scoped = self.scopes.get(name)
        if scoped is not None:
            shape = replace(
                scoped.shape,
                bytes=Affine.term((scoped.scope, Quantity.BYTES)),
                values=Affine.term((scoped.scope, Quantity.VALUES)),
            )
            return NodeCost(shape, self.node_steps(), ZERO)
        return NodeCost(self.variable_shape(name), self.node_steps(), ZERO)

    def variable_shape(self, name: str) -> Shape:
        known = self.variables.get(name)
        if known is not None:
            return known
        variable = self.environment.get(name, self.default)
        if not is_variable(variable):
            raise BoundError("an environment entry is not a variable")
        check_type(variable.type)
        shape = self.bounds.shape(variable.type, variable.bound)
        self.variables[name] = shape
        return shape

    def binary(self, node: Binary) -> NodeCost:
        left = self.visit(node.left)
        right = self.visit(node.right)
        operator = node.operator
        held, peak = held_while([left, right])
        steps = self.node_steps(left.steps, right.steps)
        if operator in LOGICAL_OPERATORS:
            return NodeCost(scalar(BOOL, 5), steps, peak)
        if operator in COMPARISON_OPERATORS:
            compared = left.shape.bytes.plus(right.shape.bytes)
            counted = left.shape.values.plus(right.shape.values)
            steps = steps.plus(compared.times(self.weights.compare_byte))
            steps = steps.plus(counted.times(self.weights.compare_value))
            return NodeCost(scalar(BOOL, 5), steps, peak)
        concatenates = operator is BinaryOperator.ADD and (
            is_concatenable(left.shape.type) or is_concatenable(right.shape.type)
        )
        if not concatenates:
            return NodeCost(scalar(left.shape.type), steps, peak)
        shape = concatenated(left.shape, right.shape)
        if not (
            is_typed_sequence(left.shape.type) and is_typed_sequence(right.shape.type)
        ):
            shape = replace(shape, bytes=shape.bytes.join(Affine.of(SCALAR_BYTES)))
        return self.collection(shape, steps, held, peak)

    def sequence(
        self, nodes: list[Node]
    ) -> tuple[list[NodeCost], Affine, Affine, Affine]:
        costs = [self.visit(child) for child in nodes]
        steps = self.node_steps(*(cost.steps for cost in costs))
        held, peak = held_while(costs)
        return costs, steps, held, peak

    def collection(
        self, shape: Shape, steps: Affine, held: Affine, peak: Affine
    ) -> NodeCost:
        steps = steps.plus(shape.bytes.times(self.weights.concat_byte))
        steps = steps.plus(shape.values.times(self.weights.concat_value))
        allocated = self.allocation(shape.bytes)
        return NodeCost(shape, steps, peak.join(held.plus(allocated)), allocated)

    def list_literal(self, elements: tuple[Node, ...]) -> NodeCost:
        costs, steps, held, peak = self.sequence(list(elements))
        shapes = [cost.shape for cost in costs]
        element = join_all(shapes)
        element_cel_type = element.type if element is not None else DYN
        size = sum_affine([shape.bytes for shape in shapes]).plus(
            Affine.of(max(2, len(shapes) + 1))
        )
        values = sum_affine([shape.values for shape in shapes]).plus(Affine.of(1))
        shape = Shape(ListOf(element_cel_type), size, values, len(shapes), 0, element)
        return self.collection(shape, steps, held, peak)

    def map_literal(self, parts: list[Node]) -> NodeCost:
        costs, steps, held, peak = self.sequence(parts)
        shapes = [cost.shape for cost in costs]
        values_only = shapes[1::2]
        element = join_all(values_only)
        size = sum_affine([shape.bytes for shape in shapes]).plus(
            Affine.of(max(2, len(shapes) + 1))
        )
        values = sum_affine([shape.values for shape in shapes]).plus(Affine.of(1))
        value_type = element.type if element is not None else DYN
        shape = Shape(
            MapOf(DYN, value_type), size, values, len(values_only), 0, element
        )
        return self.collection(shape, steps, held, peak)

    def call(self, node: Call) -> NodeCost:
        receiver = self.visit(node.receiver) if node.receiver is not None else None
        arguments = [self.visit(argument) for argument in node.arguments]
        operands = ([receiver] if receiver is not None else []) + arguments
        form = Form.GLOBAL if node.receiver is None else Form.RECEIVER
        function = lookup(node.function, len(node.arguments), form)
        if function is None:
            raise fail(Reason.UNKNOWN_FUNCTION, node.offset)
        steps = self.node_steps(*(operand.steps for operand in operands))
        held, peak = held_while(operands)
        operand_bytes = sum_affine([operand.shape.bytes for operand in operands])
        if function.charge is Charge.OPERAND_SIZE:
            steps = steps.plus(operand_bytes.times(self.weights.function_byte))
        if function.charge is Charge.PATTERN and receiver is not None:
            pattern = node.arguments[0]
            compiled = (
                parse_pattern(pattern.value, self.meter)
                if isinstance(pattern, StringLiteral)
                else 0
            )
            subject = receiver.shape.bytes
            steps = steps.plus(Affine.of(compiled * self.weights.pattern_compile))
            steps = steps.plus(subject.times(compiled * self.weights.pattern_match))
            peak = peak.join(held.plus(self.allocation(Affine.of(compiled))))
        if function.preserves_type and operands:
            first = operands[0]
            return NodeCost(
                replace(first.shape, type=DYN), steps, peak, first.allocated
            )
        if node.function in ("string", "bytes") and operands:
            result_type = STRING if node.function == "string" else BYTES
            expansion = 6 if node.function == "string" else 1
            size = operand_bytes.times(expansion).plus(Affine.of(SCALAR_BYTES))
            shape = Shape(result_type, size, Affine.of(1), 0, 0)
            allocated = self.allocation(size)
            return NodeCost(shape, steps, peak.join(held.plus(allocated)), allocated)
        return NodeCost(scalar(function.result), steps, peak)

    def comprehension(self, node: Comprehension) -> NodeCost:
        source = self.visit(node.range)
        count = source.shape.items
        element = source.shape.iterated()
        scope = self.next_scope
        self.next_scope += 1
        bytes_key: Key = (scope, Quantity.BYTES)
        values_key: Key = (scope, Quantity.VALUES)
        shadowed = self.scopes.get(node.variable)
        self.scopes[node.variable] = ScopeVariable(scope, element)
        body = [self.visit(argument) for argument in node.arguments]
        if shadowed is None:
            del self.scopes[node.variable]
        else:
            self.scopes[node.variable] = shadowed

        def across(value: Affine) -> Affine:
            return across_range(value, bytes_key, values_key, count, source.shape)

        def per_element(value: Affine) -> Affine:
            return value.substitute(bytes_key, element.bytes).substitute(
                values_key, element.values
            )

        body_steps = across(sum_affine([part.steps for part in body]))
        steps = self.node_steps(source.steps, body_steps)
        steps = steps.plus(Affine.of(self.weights.iteration * count))
        _, body_peak = held_while(body)
        body_memory = per_element(body_peak)
        if node.macro in (Macro.MAP, Macro.FILTER):
            steps = steps.plus(Affine.of(self.weights.map_quadratic * count * count))
        if node.macro is Macro.FILTER:
            shape = filtered(source.shape, element, count)
        elif node.macro is Macro.MAP:
            transform = body[-1].shape
            transformed_element = transform.substituted(
                bytes_key, element.bytes
            ).substituted(values_key, element.values)
            size = across(transform.bytes).plus(Affine.of(max(2, count + 1)))
            values = across(transform.values).plus(Affine.of(1))
            shape = Shape(
                ListOf(transform.type), size, values, count, 0, transformed_element
            )
        else:
            return NodeCost(
                scalar(BOOL, 5),
                steps,
                source.memory.join(source.allocated.plus(body_memory)),
            )
        allocated = self.allocation(shape.bytes)
        memory = source.memory.join(source.allocated.plus(body_memory).plus(allocated))
        return NodeCost(shape, steps, memory, allocated)


def filtered(source: Shape, element: Shape, count: int) -> Shape:
    if isinstance(unwrap(source.type), ListOf):
        return source
    return Shape(ListOf(element.type), source.bytes, source.values, count, 0, element)


def across_range(
    value: Affine, bytes_key: Key, values_key: Key, count: int, source: Shape
) -> Affine:
    if count == 0:
        return ZERO
    coefficients = value.coefficients()
    bytes_coefficient = coefficients.pop(bytes_key, 0)
    values_coefficient = coefficients.pop(values_key, 0)
    rest = Affine(value.constant, tuple(sorted(coefficients.items(), key=sort_key)))
    return (
        rest.times(count)
        .plus(source.bytes.times(bytes_coefficient))
        .plus(source.values.times(values_coefficient))
    )


def is_typed_sequence(cel_type: CelType) -> bool:
    inner = unwrap(cel_type)
    if isinstance(inner, Primitive):
        return inner.kind in (STRING.kind, BYTES.kind)
    return isinstance(inner, ListOf)


def same_type(left: CelType, right: CelType) -> bool:
    if left is right:
        return True
    if isinstance(left, Primitive) and isinstance(right, Primitive):
        return left.kind is right.kind
    return isinstance(left, Dyn) and isinstance(right, Dyn)


def members(left: Shape, right: Shape) -> Shape | None:
    if left.element is None and right.element is None:
        return None
    return join_shapes(left.member(), right.member())


def concatenated(left: Shape, right: Shape) -> Shape:
    element = members(left, right)
    cel_type = left.type if same_type(left.type, right.type) else DYN
    return Shape(
        cel_type,
        left.bytes.plus(right.bytes),
        left.values.plus(right.values),
        left.items + right.items,
        left.string_bytes + right.string_bytes,
        element,
    )


def join_shapes(left: Shape, right: Shape) -> Shape:
    element = members(left, right)
    cel_type = left.type if same_type(left.type, right.type) else DYN
    return Shape(
        cel_type,
        left.bytes.join(right.bytes),
        left.values.join(right.values),
        max(left.items, right.items),
        max(left.string_bytes, right.string_bytes),
        element,
    )


def join_all(shapes: list[Shape]) -> Shape | None:
    if not shapes:
        return None
    result = shapes[0]
    for shape in shapes[1:]:
        result = join_shapes(result, shape)
    return result


def estimate(
    node: Node,
    rendering_length: int,
    environment: Mapping[str, Variable],
    weights: Weights = PROVISIONAL_WEIGHTS,
    budget: Budget = PROVISIONAL_BUDGET,
    meter: Meter | None = None,
) -> Estimate:
    check_environment(environment)
    estimator = Estimator(environment, weights, meter if meter is not None else Meter())
    cost = estimator.visit(node)
    variables = tuple(sorted(estimator.variables))
    bound = [estimator.variables[name] for name in variables]
    converted_bytes = sum(shape.bytes.concrete() for shape in bound)
    converted_values = sum(shape.values.concrete() for shape in bound)
    base = (
        weights.evaluation
        + weights.convert_byte * converted_bytes
        + weights.convert_value * converted_values
    )
    steps = base + cost.steps.concrete()
    memory = (
        weights.memory_base
        + converted_bytes * weights.memory_byte
        + cost.memory.concrete()
    )
    if memory > budget.memory:
        raise fail(Reason.MEMORY, node.offset)
    if steps > budget.steps:
        raise fail(Reason.STEPS, node.offset)
    retained, bound_bytes = binding_bounds(
        {name: estimator.variables[name] for name in variables if name in environment},
        estimator.meter,
    )
    return Estimate(
        steps,
        memory,
        rendering_length * weights.rendering_character,
        variables,
        retained,
        bound_bytes,
    )
