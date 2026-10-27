from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

from kora.cel.cost import BoundError, Variable, check_type, is_variable
from kora.cel.diagnostics import Reason, fail
from kora.cel.meter import Meter
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
    TYPE,
    UINT,
    CelType,
    Dyn,
    Kind,
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Record,
    is_null,
    nullable,
    unwrap,
)


class ResultKind(Enum):
    BOOL = "bool"
    STRING = "string"


@dataclass(frozen=True)
class Environment:
    variables: Mapping[str, Variable]
    result: ResultKind
    selection_only: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Typed:
    type: CelType
    path: int | None = None


Step = str | int
VARIABLE_ROOT: Final[int] = -1
SCOPE_ROOT: Final[int] = -2
TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {"int", "uint", "double", "bool", "string", "bytes", "list", "map"}
    | {"null_type", "type"}
)
NUMERIC: Final[frozenset[Kind]] = frozenset({Kind.INT, Kind.UINT, Kind.DOUBLE})
ORDERED: Final[frozenset[Kind]] = NUMERIC | {
    Kind.STRING,
    Kind.BYTES,
    Kind.BOOL,
    Kind.TIMESTAMP,
    Kind.DURATION,
}
KEY_KINDS: Final[frozenset[Kind]] = frozenset(
    {Kind.INT, Kind.UINT, Kind.BOOL, Kind.STRING}
)
COMPARISONS: Final[frozenset[BinaryOperator]] = frozenset(
    {
        BinaryOperator.LESS,
        BinaryOperator.LESS_EQUAL,
        BinaryOperator.GREATER,
        BinaryOperator.GREATER_EQUAL,
    }
)
SAME_NUMERIC: Final[tuple[tuple[Kind, Kind, Kind], ...]] = tuple(
    (kind, kind, kind) for kind in NUMERIC
)
ARITHMETIC: Final[dict[BinaryOperator, tuple[tuple[Kind, Kind, Kind], ...]]] = {
    BinaryOperator.ADD: (
        *SAME_NUMERIC,
        (Kind.STRING, Kind.STRING, Kind.STRING),
        (Kind.BYTES, Kind.BYTES, Kind.BYTES),
        (Kind.TIMESTAMP, Kind.DURATION, Kind.TIMESTAMP),
        (Kind.DURATION, Kind.DURATION, Kind.DURATION),
    ),
    BinaryOperator.SUBTRACT: (
        *SAME_NUMERIC,
        (Kind.TIMESTAMP, Kind.TIMESTAMP, Kind.DURATION),
        (Kind.TIMESTAMP, Kind.DURATION, Kind.TIMESTAMP),
        (Kind.DURATION, Kind.DURATION, Kind.DURATION),
    ),
    BinaryOperator.MULTIPLY: SAME_NUMERIC,
    BinaryOperator.DIVIDE: SAME_NUMERIC,
    BinaryOperator.MODULO: tuple((kind, kind, kind) for kind in (Kind.INT, Kind.UINT)),
}
CONVERTIBLE: Final[frozenset[Kind]] = NUMERIC | {Kind.STRING}
TIME_ACCESSORS: Final[frozenset[str]] = frozenset(
    {"getHours", "getMinutes", "getSeconds"}
)
DATE_ACCESSORS: Final[frozenset[str]] = frozenset(
    {"getDate", "getDayOfMonth", "getDayOfWeek", "getDayOfYear", "getFullYear"}
    | {"getMonth", "getMilliseconds"}
)
SIGNATURES: Final[dict[str, tuple[tuple[tuple[Kind, ...], Kind], ...]]] = {
    "int": tuple(((kind,), Kind.INT) for kind in CONVERTIBLE),
    "uint": tuple(((kind,), Kind.UINT) for kind in CONVERTIBLE),
    "double": tuple(((kind,), Kind.DOUBLE) for kind in CONVERTIBLE),
    "string": tuple(
        ((kind,), Kind.STRING)
        for kind in CONVERTIBLE | {Kind.BYTES, Kind.TIMESTAMP, Kind.DURATION}
    ),
    "bytes": (((Kind.STRING,), Kind.BYTES), ((Kind.BYTES,), Kind.BYTES)),
    "duration": (((Kind.STRING,), Kind.DURATION), ((Kind.DURATION,), Kind.DURATION)),
    "timestamp": (
        ((Kind.STRING,), Kind.TIMESTAMP),
        ((Kind.TIMESTAMP,), Kind.TIMESTAMP),
    ),
    "contains": (((Kind.STRING, Kind.STRING), Kind.BOOL),),
    "startsWith": (((Kind.STRING, Kind.STRING), Kind.BOOL),),
    "endsWith": (((Kind.STRING, Kind.STRING), Kind.BOOL),),
    "matches": (((Kind.STRING, Kind.STRING), Kind.BOOL),),
    **{name: (((Kind.TIMESTAMP,), Kind.INT),) for name in DATE_ACCESSORS},
    **{
        name: (((Kind.TIMESTAMP,), Kind.INT), ((Kind.DURATION,), Kind.INT))
        for name in TIME_ACCESSORS
    },
}
EXACT: Final[frozenset[str]] = frozenset({"getMilliseconds"})
LITERAL_TYPES: Final[dict[type, CelType]] = {
    IntLiteral: INT,
    UintLiteral: UINT,
    DoubleLiteral: DOUBLE,
    StringLiteral: STRING,
    BytesLiteral: BYTES,
    BoolLiteral: BOOL,
    NullLiteral: NULL,
}


def kind_of(cel_type: CelType) -> Kind | None:
    inner = unwrap(cel_type)
    return inner.kind if isinstance(inner, Primitive) else None


def plain(kind: Kind) -> CelType:
    return Primitive(kind)


def join(left: CelType, right: CelType) -> CelType:
    if left is right or isinstance(left, Dyn):
        return left
    if isinstance(right, Dyn):
        return right
    if is_null(left) or is_null(right):
        return nullable(right if is_null(left) else left)
    wrapped = isinstance(left, Nullable) or isinstance(right, Nullable)
    joined = shallow_join(unwrap(left), unwrap(right))
    return nullable(joined) if wrapped else joined


def shallow_join(left: CelType, right: CelType) -> CelType:
    if left is right:
        return left
    if isinstance(left, Primitive) and isinstance(right, Primitive):
        if left.kind is not right.kind:
            return DYN
        return left if left.allowed is right.allowed else plain(left.kind)
    if isinstance(left, ListOf) and isinstance(right, ListOf):
        return ListOf(scalar_join(left.element, right.element))
    if isinstance(left, MapOf) and isinstance(right, MapOf):
        return MapOf(
            scalar_join(left.key, right.key), scalar_join(left.value, right.value)
        )
    return DYN


def scalar_join(left: CelType, right: CelType) -> CelType:
    if left is right:
        return left
    kinds = (kind_of(left), kind_of(right))
    nested = isinstance(left, Nullable) or isinstance(right, Nullable)
    if kinds[0] is None or kinds[0] is not kinds[1] or nested:
        return DYN
    return plain(kinds[0])


def equatable(left: CelType, right: CelType) -> bool:
    if isinstance(left, Dyn) or isinstance(right, Dyn):
        return True
    if is_null(left) or is_null(right):
        other = right if is_null(left) else left
        return isinstance(other, Nullable) or is_null(other)
    inner_left, inner_right = unwrap(left), unwrap(right)
    if isinstance(inner_left, Primitive) and isinstance(inner_right, Primitive):
        return inner_left.kind is inner_right.kind
    if isinstance(inner_left, ListOf) and isinstance(inner_right, ListOf):
        return True
    keyed = (MapOf, Record)
    return isinstance(inner_left, keyed) and isinstance(inner_right, keyed)


def is_key(cel_type: CelType) -> bool:
    if isinstance(cel_type, Dyn):
        return True
    return isinstance(cel_type, Primitive) and cel_type.kind in KEY_KINDS


def literal_step(node: Node) -> Step | None:
    if isinstance(node, StringLiteral | IntLiteral):
        return node.value
    return None


def literal_key(node: Node) -> tuple[str, object] | None:
    if isinstance(node, BoolLiteral | StringLiteral | IntLiteral | UintLiteral):
        return type(node).__name__, node.value
    return None


class Checker:
    def __init__(self, environment: Environment, meter: Meter) -> None:
        self.environment: Environment = environment
        self.meter: Meter = meter
        self.scopes: dict[str, Typed] = {}
        self.paths: dict[tuple[int, Step], int] = {}
        self.facts: dict[int, int] = {}
        self.scope_count: int = 0
        self.checked: set[str] = set()

    def key(self, parent: int | None, step: Step | None) -> int | None:
        if parent is None or step is None:
            return None
        self.meter.tick()
        return self.paths.setdefault((parent, step), len(self.paths))

    def visit(self, node: Node) -> Typed:
        self.meter.tick()
        match node:
            case Identifier():
                return self.identifier(node, selected=False)
            case Select():
                return self.select(node)
            case Index():
                return self.index(node)
            case Unary():
                return self.unary(node)
            case Binary():
                return self.binary(node)
            case Conditional():
                return self.conditional(node)
            case ListLiteral(elements=elements):
                return Typed(ListOf(self.joined(list(elements))))
            case MapLiteral():
                return self.map_literal(node)
            case Has():
                return self.has(node)
            case Call():
                return self.call(node)
            case Comprehension():
                return self.comprehension(node)
            case _:
                return Typed(LITERAL_TYPES[type(node)])

    def operand(self, node: Node) -> Typed:
        if isinstance(node, Identifier):
            return self.identifier(node, selected=True)
        return self.visit(node)

    def identifier(self, node: Identifier, selected: bool) -> Typed:
        name = node.name
        scoped = self.scopes.get(name)
        if scoped is not None:
            return scoped
        variable = self.environment.variables.get(name)
        if variable is not None:
            if name in self.environment.selection_only and not selected:
                raise fail(Reason.ROOT_NOT_SELECTED, node.offset)
            return Typed(self.variable_type(name, variable), self.root(name))
        if name in TYPE_NAMES:
            return Typed(TYPE)
        raise fail(Reason.UNKNOWN_IDENTIFIER, node.offset)

    def variable_type(self, name: str, variable: object) -> CelType:
        if not isinstance(variable, Variable) or not is_variable(variable):
            raise BoundError("an environment entry is not a variable")
        if name not in self.checked:
            check_type(variable.type)
            self.checked.add(name)
        return variable.type

    def root(self, name: str) -> int | None:
        return self.key(VARIABLE_ROOT, name)

    def require(self, path: int | None, offset: int) -> None:
        if path is None or path not in self.facts:
            raise fail(Reason.UNGUARDED_OPTIONAL, offset)

    def select(self, node: Select) -> Typed:
        inner = self.operand(node.operand)
        path = self.key(inner.path, node.name)
        container = unwrap(inner.type)
        if isinstance(container, Dyn):
            return Typed(DYN, path)
        if isinstance(container, MapOf):
            raise fail(Reason.UNKNOWN_FIELD, node.offset)
        if not isinstance(container, Record):
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        declared = container.fields.get(node.name)
        if declared is None:
            raise fail(Reason.UNKNOWN_FIELD, node.offset)
        if not declared.required:
            self.require(path, node.offset)
        return Typed(declared.type, path)

    def index(self, node: Index) -> Typed:
        inner = self.visit(node.operand)
        key = self.visit(node.index)
        step = literal_step(node.index)
        path = self.key(inner.path, step)
        container = unwrap(inner.type)
        key_kind = kind_of(key.type)
        dynamic_key = isinstance(key.type, Dyn)
        if isinstance(container, Dyn):
            return Typed(DYN, path)
        if isinstance(container, ListOf):
            if not dynamic_key and key_kind is not Kind.INT:
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
            return Typed(container.element, path)
        if isinstance(container, MapOf):
            if not equatable(container.key, key.type):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
            if step is not None:
                self.require(path, node.offset)
            return Typed(container.value, path)
        if not isinstance(container, Record):
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        if not dynamic_key and key_kind is not Kind.STRING:
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        if not isinstance(step, str):
            return Typed(DYN)
        declared = container.fields.get(step)
        if declared is not None:
            if not declared.required:
                self.require(path, node.offset)
            return Typed(declared.type, path)
        if container.extra is None:
            raise fail(Reason.UNKNOWN_FIELD, node.offset)
        self.require(path, node.offset)
        return Typed(container.extra, path)

    def has(self, node: Has) -> Typed:
        target = node.target
        container = unwrap(self.operand(target.operand).type)
        if isinstance(container, MapOf):
            raise fail(Reason.UNKNOWN_FIELD, target.offset)
        if isinstance(container, Record):
            if target.name not in container.fields:
                raise fail(Reason.UNKNOWN_FIELD, target.offset)
        elif not isinstance(container, Dyn):
            raise fail(Reason.NO_MATCHING_OVERLOAD, target.offset)
        return Typed(BOOL)

    def path_of(self, node: Node) -> int | None:
        if isinstance(node, Identifier):
            scoped = self.scopes.get(node.name)
            if scoped is not None:
                return scoped.path
            known = node.name in self.environment.variables
            return self.root(node.name) if known else None
        if isinstance(node, Select):
            return self.key(self.path_of(node.operand), node.name)
        if isinstance(node, Index):
            return self.key(self.path_of(node.operand), literal_step(node.index))
        return None

    def true_facts(self, node: Node) -> list[int]:
        match node:
            case Has(target=target):
                path = self.path_of(target)
                return [path] if path is not None else []
            case Binary(operator=BinaryOperator.IN, left=left, right=right):
                path = self.key(self.path_of(right), literal_step(left))
                return [path] if path is not None else []
            case Binary(operator=BinaryOperator.AND, left=left, right=right):
                return self.true_facts(left) + self.true_facts(right)
            case Unary(operator=UnaryOperator.NOT, operand=operand):
                return self.false_facts(operand)
            case _:
                return []

    def false_facts(self, node: Node) -> list[int]:
        match node:
            case Binary(operator=BinaryOperator.OR, left=left, right=right):
                return self.false_facts(left) + self.false_facts(right)
            case Unary(operator=UnaryOperator.NOT, operand=operand):
                return self.true_facts(operand)
            case _:
                return []

    def guarded(self, facts: list[int], node: Node) -> Typed:
        for fact in facts:
            self.meter.tick()
            self.facts[fact] = self.facts.get(fact, 0) + 1
        result = self.visit(node)
        for fact in facts:
            remaining = self.facts[fact] - 1
            if remaining:
                self.facts[fact] = remaining
            else:
                del self.facts[fact]
        return result

    def boolean(self, typed: Typed, offset: int) -> None:
        if not isinstance(typed.type, Dyn) and kind_of(typed.type) is not Kind.BOOL:
            raise fail(Reason.NO_MATCHING_OVERLOAD, offset)

    def unary(self, node: Unary) -> Typed:
        inner = self.visit(node.operand)
        if node.operator is UnaryOperator.NOT:
            self.boolean(inner, node.offset)
            return Typed(BOOL)
        kind = kind_of(inner.type)
        if kind not in (Kind.INT, Kind.DOUBLE):
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        return Typed(unwrap(inner.type))

    def conditional(self, node: Conditional) -> Typed:
        self.boolean(self.visit(node.condition), node.offset)
        then = self.guarded(self.true_facts(node.condition), node.then)
        otherwise = self.visit(node.otherwise)
        return Typed(join(then.type, otherwise.type))

    def binary(self, node: Binary) -> Typed:
        operator = node.operator
        left = self.visit(node.left)
        if operator in (BinaryOperator.AND, BinaryOperator.OR):
            self.boolean(left, node.offset)
            and_chain = operator is BinaryOperator.AND
            facts = (
                self.true_facts(node.left) if and_chain else self.false_facts(node.left)
            )
            self.boolean(self.guarded(facts, node.right), node.offset)
            return Typed(BOOL)
        right = self.visit(node.right)
        if operator in (BinaryOperator.EQUAL, BinaryOperator.NOT_EQUAL):
            if not equatable(left.type, right.type):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
            self.enum_literal(left.type, node.right)
            self.enum_literal(right.type, node.left)
            return Typed(BOOL)
        if operator in COMPARISONS:
            if not ordered(left.type, right.type):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
            return Typed(BOOL)
        if operator is BinaryOperator.IN:
            self.membership(node, left, right)
            return Typed(BOOL)
        return Typed(self.arithmetic(node, left.type, right.type))

    def enum_literal(self, cel_type: CelType, node: Node) -> None:
        inner = unwrap(cel_type)
        allowed = inner.allowed if isinstance(inner, Primitive) else None
        if allowed is not None and isinstance(node, StringLiteral):
            if node.value not in allowed:
                raise fail(Reason.UNKNOWN_ENUM_VALUE, node.offset)

    def membership(self, node: Binary, left: Typed, right: Typed) -> None:
        container = unwrap(right.type)
        if isinstance(container, ListOf):
            if not equatable(left.type, container.element):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
            self.enum_literal(container.element, node.left)
            if isinstance(node.right, ListLiteral):
                for element in node.right.elements:
                    self.enum_literal(left.type, element)
        elif isinstance(container, MapOf):
            if not is_key(left.type) or not equatable(left.type, container.key):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        elif isinstance(container, Record):
            if not equatable(left.type, STRING):
                raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        elif not isinstance(container, Dyn):
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)

    def arithmetic(self, node: Binary, left: CelType, right: CelType) -> CelType:
        inner_left, inner_right = unwrap(left), unwrap(right)
        if node.operator is BinaryOperator.ADD:
            lists = (isinstance(inner_left, ListOf), isinstance(inner_right, ListOf))
            if all(lists):
                return join(inner_left, inner_right)
            if any(lists) and (
                isinstance(inner_left, Dyn) or isinstance(inner_right, Dyn)
            ):
                return DYN
        overloads = ARITHMETIC[node.operator]
        left_kind, right_kind = kind_of(left), kind_of(right)
        for first, second, result in overloads:
            left_fits = isinstance(inner_left, Dyn) or left_kind is first
            right_fits = isinstance(inner_right, Dyn) or right_kind is second
            if left_fits and right_fits:
                dynamic = isinstance(inner_left, Dyn) or isinstance(inner_right, Dyn)
                return DYN if dynamic else plain(result)
        raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)

    def joined(self, nodes: list[Node]) -> CelType:
        result: CelType | None = None
        for child in nodes:
            typed = self.visit(child).type
            result = typed if result is None else join(result, typed)
        return DYN if result is None else result

    def map_literal(self, node: MapLiteral) -> Typed:
        seen: set[tuple[str, object]] = set()
        for entry in node.entries:
            literal = literal_key(entry.key)
            if literal is not None:
                if literal in seen:
                    raise fail(Reason.REPEATED_MAP_KEY, entry.key.offset)
                seen.add(literal)
        keys: CelType | None = None
        for entry in node.entries:
            key = self.visit(entry.key).type
            if not is_key(key):
                raise fail(Reason.NO_MATCHING_OVERLOAD, entry.key.offset)
            keys = key if keys is None else join(keys, key)
        values = self.joined([entry.value for entry in node.entries])
        return Typed(MapOf(DYN if keys is None else keys, values))

    def call(self, node: Call) -> Typed:
        receiver = [node.receiver] if node.receiver is not None else []
        operands = [self.visit(part) for part in (*receiver, *node.arguments)]
        name = node.function
        if name == "type":
            return Typed(TYPE)
        if name == "dyn":
            return Typed(DYN)
        if name == "size":
            return self.size(operands[0], node.offset)
        for kinds, result in SIGNATURES[name]:
            if all(
                kind_of(operand.type) is kind
                or (isinstance(unwrap(operand.type), Dyn) and name not in EXACT)
                for operand, kind in zip(operands, kinds, strict=True)
            ):
                return Typed(plain(result))
        raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)

    def size(self, operand: Typed, offset: int) -> Typed:
        inner = unwrap(operand.type)
        if isinstance(inner, Dyn) or kind_of(inner) is Kind.STRING:
            raise fail(Reason.STRING_SIZE, offset)
        if isinstance(inner, ListOf | MapOf | Record) or kind_of(inner) is Kind.BYTES:
            return Typed(INT)
        raise fail(Reason.NO_MATCHING_OVERLOAD, offset)

    def comprehension(self, node: Comprehension) -> Typed:
        source = unwrap(self.visit(node.range).type)
        if isinstance(source, ListOf):
            element = source.element
        elif isinstance(source, MapOf):
            element = source.key
        elif isinstance(source, Record):
            element = STRING
        elif isinstance(source, Dyn):
            element = DYN
        else:
            raise fail(Reason.NO_MATCHING_OVERLOAD, node.offset)
        if node.variable in self.environment.variables:
            raise fail(Reason.SHADOWED_VARIABLE, node.offset)
        self.scope_count += 1
        shadowed = self.scopes.get(node.variable)
        self.scopes[node.variable] = Typed(
            element, self.key(SCOPE_ROOT, self.scope_count)
        )
        body = [self.visit(part) for part in node.arguments]
        if shadowed is None:
            del self.scopes[node.variable]
        else:
            self.scopes[node.variable] = shadowed
        if node.macro is Macro.MAP:
            if len(body) == 2:
                self.boolean(body[0], node.offset)
            return Typed(ListOf(body[-1].type))
        self.boolean(body[0], node.offset)
        if node.macro is Macro.FILTER:
            return Typed(ListOf(element))
        return Typed(BOOL)


def ordered(left: CelType, right: CelType) -> bool:
    kinds = (kind_of(left), kind_of(right))
    dynamic = (isinstance(unwrap(left), Dyn), isinstance(unwrap(right), Dyn))
    if all(dynamic):
        return True
    if any(dynamic):
        known = kinds[1] if dynamic[0] else kinds[0]
        return known in ORDERED
    if kinds[0] in NUMERIC and kinds[1] in NUMERIC:
        return True
    return kinds[0] is kinds[1] and kinds[0] in ORDERED


def check_types(tree: Node, environment: Environment, meter: Meter) -> CelType:
    result = Checker(environment, meter).visit(tree).type
    expected = Kind.BOOL if environment.result is ResultKind.BOOL else Kind.STRING
    if not isinstance(result, Dyn) and (
        isinstance(result, Nullable) or kind_of(result) is not expected
    ):
        raise fail(Reason.WRONG_RESULT_TYPE, 0)
    return result
