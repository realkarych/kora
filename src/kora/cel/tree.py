import struct
from dataclasses import dataclass, field
from enum import Enum


class UnaryOperator(Enum):
    NOT = "!"
    NEGATE = "-"


class BinaryOperator(Enum):
    OR = "||"
    AND = "&&"
    EQUAL = "=="
    NOT_EQUAL = "!="
    LESS = "<"
    LESS_EQUAL = "<="
    GREATER = ">"
    GREATER_EQUAL = ">="
    IN = "in"
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    MODULO = "%"


class Macro(Enum):
    ALL = "all"
    EXISTS = "exists"
    EXISTS_ONE = "exists_one"
    MAP = "map"
    FILTER = "filter"


@dataclass(frozen=True)
class IntLiteral:
    value: int
    offset: int = field(compare=False)


@dataclass(frozen=True)
class UintLiteral:
    value: int
    offset: int = field(compare=False)


def double_bits(value: float) -> int:
    packed: tuple[int] = struct.unpack("<q", struct.pack("<d", value))
    return packed[0]


@dataclass(frozen=True)
class DoubleLiteral:
    value: float = field(compare=False)
    offset: int = field(compare=False)
    bits: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bits", double_bits(self.value))


@dataclass(frozen=True)
class StringLiteral:
    value: str
    offset: int = field(compare=False)


@dataclass(frozen=True)
class BytesLiteral:
    value: bytes
    offset: int = field(compare=False)


@dataclass(frozen=True)
class BoolLiteral:
    value: bool
    offset: int = field(compare=False)


@dataclass(frozen=True)
class NullLiteral:
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Identifier:
    name: str
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Select:
    operand: "Node"
    name: str
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Index:
    operand: "Node"
    index: "Node"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Call:
    function: str
    receiver: "Node | None"
    arguments: "tuple[Node, ...]"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class ListLiteral:
    elements: "tuple[Node, ...]"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class MapEntry:
    key: "Node"
    value: "Node"


@dataclass(frozen=True)
class MapLiteral:
    entries: tuple[MapEntry, ...]
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Unary:
    operator: UnaryOperator
    operand: "Node"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Binary:
    operator: BinaryOperator
    left: "Node"
    right: "Node"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Conditional:
    condition: "Node"
    then: "Node"
    otherwise: "Node"
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Has:
    target: Select
    offset: int = field(compare=False)


@dataclass(frozen=True)
class Comprehension:
    macro: Macro
    range: "Node"
    variable: str
    arguments: "tuple[Node, ...]"
    offset: int = field(compare=False)


Literal = (
    IntLiteral
    | UintLiteral
    | DoubleLiteral
    | StringLiteral
    | BytesLiteral
    | BoolLiteral
    | NullLiteral
)

Node = (
    Literal
    | Identifier
    | Select
    | Index
    | Call
    | ListLiteral
    | MapLiteral
    | Unary
    | Binary
    | Conditional
    | Has
    | Comprehension
)
