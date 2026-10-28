from dataclasses import dataclass
from enum import Enum
from typing import Final

from kora.cel.types import (
    BOOL,
    BYTES,
    DOUBLE,
    DURATION,
    DYN,
    INT,
    STRING,
    TIMESTAMP,
    TYPE,
    UINT,
    CelType,
)


class Form(Enum):
    GLOBAL = "global"
    RECEIVER = "receiver"


class Charge(Enum):
    CONSTANT = "constant"
    OPERAND_SIZE = "operand_size"
    PATTERN = "pattern"


@dataclass(frozen=True)
class Function:
    name: str
    arity: int
    form: Form
    result: CelType
    charge: Charge
    preserves_type: bool = False


ACCESSORS: Final[tuple[str, ...]] = (
    "getDate",
    "getDayOfMonth",
    "getDayOfWeek",
    "getDayOfYear",
    "getFullYear",
    "getHours",
    "getMilliseconds",
    "getMinutes",
    "getMonth",
    "getSeconds",
)

STANDARD_FUNCTIONS: Final[tuple[Function, ...]] = (
    Function("size", 1, Form.GLOBAL, INT, Charge.CONSTANT),
    Function("size", 0, Form.RECEIVER, INT, Charge.CONSTANT),
    Function("int", 1, Form.GLOBAL, INT, Charge.OPERAND_SIZE),
    Function("uint", 1, Form.GLOBAL, UINT, Charge.OPERAND_SIZE),
    Function("double", 1, Form.GLOBAL, DOUBLE, Charge.OPERAND_SIZE),
    Function("string", 1, Form.GLOBAL, STRING, Charge.OPERAND_SIZE),
    Function("bytes", 1, Form.GLOBAL, BYTES, Charge.OPERAND_SIZE),
    Function("type", 1, Form.GLOBAL, TYPE, Charge.CONSTANT),
    Function("dyn", 1, Form.GLOBAL, DYN, Charge.CONSTANT, preserves_type=True),
    Function("duration", 1, Form.GLOBAL, DURATION, Charge.OPERAND_SIZE),
    Function("timestamp", 1, Form.GLOBAL, TIMESTAMP, Charge.OPERAND_SIZE),
    Function("contains", 1, Form.RECEIVER, BOOL, Charge.OPERAND_SIZE),
    Function("endsWith", 1, Form.RECEIVER, BOOL, Charge.OPERAND_SIZE),
    Function("startsWith", 1, Form.RECEIVER, BOOL, Charge.OPERAND_SIZE),
    Function("matches", 1, Form.RECEIVER, BOOL, Charge.PATTERN),
    *(Function(name, 0, Form.RECEIVER, INT, Charge.CONSTANT) for name in ACCESSORS),
)

FUNCTIONS: Final[dict[tuple[str, int, Form], Function]] = {
    (function.name, function.arity, function.form): function
    for function in STANDARD_FUNCTIONS
}

MACRO_NAMES: Final[frozenset[str]] = frozenset(
    {"all", "exists", "exists_one", "map", "filter"}
)


def lookup(name: str, arity: int, form: Form) -> Function | None:
    return FUNCTIONS.get((name, arity, form))
