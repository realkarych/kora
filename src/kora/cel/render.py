from typing import Final

from kora.cel.characters import PRINTABLE_ASCII
from kora.cel.diagnostics import Reason, fail
from kora.cel.parser import RENDERING_LIMITS, parse
from kora.cel.tree import (
    Binary,
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
    MapLiteral,
    Node,
    NullLiteral,
    Select,
    StringLiteral,
    UintLiteral,
    Unary,
)

SHORT_ESCAPES: Final[dict[int, str]] = {
    0x5C: "\\\\",
    0x22: '\\"',
    0x0A: "\\n",
    0x0D: "\\r",
    0x09: "\\t",
}

RENDERING_EXPANSION: Final[int] = 10


def render_string(value: str) -> str:
    parts: list[str] = ['"']
    for character in value:
        point = ord(character)
        if point in SHORT_ESCAPES:
            parts.append(SHORT_ESCAPES[point])
        elif point in PRINTABLE_ASCII:
            parts.append(character)
        elif point <= 0xFFFF:
            parts.append(f"\\u{point:04x}")
        else:
            parts.append(f"\\U{point:08x}")
    parts.append('"')
    return "".join(parts)


def render_bytes(value: bytes) -> str:
    parts: list[str] = ['b"']
    for point in value:
        if point in SHORT_ESCAPES:
            parts.append(SHORT_ESCAPES[point])
        elif point in PRINTABLE_ASCII:
            parts.append(chr(point))
        else:
            parts.append(f"\\x{point:02x}")
    parts.append('"')
    return "".join(parts)


def render_double(value: float) -> str:
    return repr(value)


def is_numeric_literal(node: Node) -> bool:
    return isinstance(node, IntLiteral | DoubleLiteral | UintLiteral)


def is_negative_literal(node: Node) -> bool:
    if isinstance(node, IntLiteral):
        return node.value < 0
    if isinstance(node, DoubleLiteral):
        return str(node.value).startswith("-")
    return False


def render_operand(node: Node) -> str:
    text = render(node)
    return f"({text})" if is_negative_literal(node) else text


def render(node: Node) -> str:
    match node:
        case IntLiteral(value=value):
            return str(value)
        case UintLiteral(value=value):
            return f"{value}u"
        case DoubleLiteral(value=value):
            return render_double(value)
        case StringLiteral(value=value):
            return render_string(value)
        case BytesLiteral(value=value):
            return render_bytes(value)
        case BoolLiteral(value=value):
            return "true" if value else "false"
        case NullLiteral():
            return "null"
        case Identifier(name=name):
            return name
        case Select(operand=operand, name=field):
            return f"{render_operand(operand)}.{field}"
        case Index(operand=operand, index=index):
            return f"{render_operand(operand)}[{render(index)}]"
        case Call(function=function, receiver=receiver, arguments=arguments):
            joined = ", ".join(render(argument) for argument in arguments)
            if receiver is None:
                return f"{function}({joined})"
            return f"{render_operand(receiver)}.{function}({joined})"
        case ListLiteral(elements=elements):
            return "[" + ", ".join(render(element) for element in elements) + "]"
        case MapLiteral(entries=entries):
            joined = ", ".join(
                f"{render(entry.key)}: {render(entry.value)}" for entry in entries
            )
            return "{" + joined + "}"
        case Unary(operator=operator, operand=operand):
            inner = render(operand)
            if is_numeric_literal(operand):
                inner = f"({inner})"
            return f"({operator.value}{inner})"
        case Binary(operator=operator, left=left, right=right):
            return f"({render(left)} {operator.value} {render(right)})"
        case Conditional(condition=condition, then=then, otherwise=otherwise):
            return f"({render(condition)} ? {render(then)} : {render(otherwise)})"
        case Has(target=target):
            return f"has({render(target)})"
        case Comprehension(
            macro=macro, range=range_, variable=variable, arguments=arguments
        ):
            joined = ", ".join(
                [variable, *(render(argument) for argument in arguments)]
            )
            return f"{render_operand(range_)}.{macro.value}({joined})"


def canonical_rendering(node: Node) -> str:
    rendering = render(node)
    if parse(rendering, RENDERING_LIMITS) != node:
        raise fail(Reason.RENDERING_MISMATCH, 0)
    return rendering
