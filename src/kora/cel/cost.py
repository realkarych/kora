import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

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
    ListOf,
    MapOf,
    element_type,
    is_concatenable,
    iteration_type,
    member_type,
    unwrap,
)

DATA_BYTES: Final[int] = 65536
DATA_ITEMS: Final[int] = 1000
DATA_STRING_BYTES: Final[int] = 16384
DATA_DEPTH: Final[int] = 32
SCALAR_BYTES: Final[int] = 24
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
    literal_value: int
    pattern_compile: int
    pattern_match: int
    rendering_character: int
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
    literal_value=30_000_000,
    pattern_compile=2_400_000,
    pattern_match=40,
    rendering_character=1_000_000_000,
    memory_byte=1,
)

PROVISIONAL_BUDGET: Final[Budget] = Budget(
    steps=10_000_000_000_000, memory=16 * 1024 * 1024
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
    values=DATA_BYTES // 2 + 1,
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


def join_affine(values: list[Affine]) -> Affine:
    result = ZERO
    for value in values:
        result = result.join(value)
    return result


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

    def hereditary(self, child_type: CelType) -> "Shape":
        return Shape(child_type, self.bytes, self.values, self.items, self.string_bytes)

    def child(self, child_type: CelType, name: str | None) -> "Shape":
        declared = self.fields.get(name) if name is not None else None
        if declared is None:
            declared = self.element
        if declared is None:
            return self.hereditary(child_type)
        return declared if child_type == DYN else replace(declared, type=child_type)

    def substituted(self, key: Key, replacement: Affine) -> "Shape":
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

    def iterated(self) -> "Shape":
        if isinstance(unwrap(self.type), ListOf):
            return self.child(iteration_type(self.type), None)
        return self.hereditary(iteration_type(self.type))


def scalar(cel_type: CelType, size: int = SCALAR_BYTES) -> Shape:
    return Shape(cel_type, Affine.of(size), Affine.of(1), 0, 0)


def from_bound(cel_type: CelType, bound: ValueBound) -> Shape:
    element = (
        from_bound(element_type(cel_type), bound.element)
        if bound.element is not None
        else None
    )
    fields = {
        name: from_bound(member_type(cel_type, name), child)
        for name, child in bound.fields.items()
    }
    return Shape(
        cel_type,
        Affine.of(bound.bytes),
        Affine.of(bound.values),
        bound.items,
        bound.string_bytes,
        element,
        fields,
    )


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
    visits: int


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
        self.variables: dict[str, None] = {}
        self.visits: int = 0

    def node_steps(self, *parts: Affine) -> Affine:
        return sum_affine([Affine.of(self.weights.node), *parts])

    def allocation(self, size: Affine) -> Affine:
        return size.times(self.weights.memory_byte)

    def visit(self, node: Node) -> NodeCost:
        self.visits += 1
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
                shape = inner.shape.child(element_type(inner.shape.type), None)
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
        self.variables[name] = None
        variable = self.environment.get(name, Variable(DYN))
        shape = from_bound(variable.type, variable.bound)
        return NodeCost(shape, self.node_steps(), ZERO)

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
        steps = steps.plus(shape.bytes.times(self.weights.concat_byte))
        steps = steps.plus(shape.values.times(self.weights.concat_value))
        allocated = self.allocation(shape.bytes)
        return NodeCost(shape, steps, peak.join(held.plus(allocated)), allocated)

    def sequence(
        self, nodes: list[Node]
    ) -> tuple[list[NodeCost], Affine, Affine, Affine]:
        costs = [self.visit(child) for child in nodes]
        steps = self.node_steps(*(cost.steps for cost in costs))
        steps = steps.plus(Affine.of(self.weights.literal_value * len(costs)))
        held, peak = held_while(costs)
        return costs, steps, held, peak

    def collection(
        self, shape: Shape, steps: Affine, held: Affine, peak: Affine
    ) -> NodeCost:
        allocated = self.allocation(shape.bytes)
        return NodeCost(shape, steps, peak.join(held.plus(allocated)), allocated)

    def list_literal(self, elements: tuple[Node, ...]) -> NodeCost:
        costs, steps, held, peak = self.sequence(list(elements))
        shapes = [cost.shape for cost in costs]
        element = join_all(shapes)
        element_cel_type = element.type if element is not None else DYN
        size = sum_affine([shape.bytes for shape in shapes]).plus(
            Affine.of(len(shapes) + 1)
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
            Affine.of(len(shapes) + 1)
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
                parse_pattern(pattern.value)
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
            size = operand_bytes.plus(Affine.of(SCALAR_BYTES))
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
            shape = source.shape
        elif node.macro is Macro.MAP:
            transform = body[-1].shape
            transformed_element = transform.substituted(
                bytes_key, element.bytes
            ).substituted(values_key, element.values)
            size = across(transform.bytes).plus(Affine.of(count + 1))
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


def across_range(
    value: Affine, bytes_key: Key, values_key: Key, count: int, source: Shape
) -> Affine:
    coefficients = value.coefficients()
    bytes_coefficient = coefficients.pop(bytes_key, 0)
    values_coefficient = coefficients.pop(values_key, 0)
    rest = Affine(value.constant, tuple(sorted(coefficients.items(), key=sort_key)))
    return (
        rest.times(count)
        .plus(source.bytes.times(bytes_coefficient))
        .plus(source.values.times(values_coefficient))
    )


def concatenated(left: Shape, right: Shape) -> Shape:
    element = join_all(
        [shape for shape in (left.element, right.element) if shape is not None]
    )
    cel_type = left.type if left.type == right.type else DYN
    return Shape(
        cel_type,
        left.bytes.plus(right.bytes),
        left.values.plus(right.values),
        left.items + right.items,
        left.string_bytes + right.string_bytes,
        element,
    )


def join_shapes(left: Shape, right: Shape) -> Shape:
    element = join_all(
        [shape for shape in (left.element, right.element) if shape is not None]
    )
    cel_type = left.type if left.type == right.type else DYN
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
    estimator = Estimator(environment, weights, meter if meter is not None else Meter())
    cost = estimator.visit(node)
    variables = tuple(sorted(estimator.variables))
    bound = [
        from_bound(
            estimator.environment.get(name, Variable(DYN)).type,
            estimator.environment.get(name, Variable(DYN)).bound,
        )
        for name in variables
    ]
    converted_bytes = sum(shape.bytes.concrete() for shape in bound)
    converted_values = sum(shape.values.concrete() for shape in bound)
    base = (
        weights.evaluation
        + weights.convert_byte * converted_bytes
        + weights.convert_value * converted_values
    )
    steps = base + cost.steps.concrete()
    memory = converted_bytes * weights.memory_byte + cost.memory.concrete()
    if memory > budget.memory:
        raise fail(Reason.MEMORY, node.offset)
    if steps > budget.steps:
        raise fail(Reason.STEPS, node.offset)
    return Estimate(
        steps,
        memory,
        rendering_length * weights.rendering_character,
        variables,
        estimator.visits,
    )
