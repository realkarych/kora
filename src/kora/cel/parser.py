from dataclasses import dataclass
from typing import Final

from kora.cel.diagnostics import DiagnosticError, Reason, fail
from kora.cel.functions import MACRO_NAMES, Form, lookup
from kora.cel.limits import (
    MAX_CODE_POINTS,
    MAX_COMPREHENSION_NESTING,
    MAX_COMPREHENSIONS,
    MAX_DEPTH,
    MAX_INT,
    MAX_RENDERING_DEPTH,
    MAX_UINT,
    MIN_INT,
)
from kora.cel.meter import Meter
from kora.cel.regex import PatternError, parse_pattern
from kora.cel.reserved import RESERVED_IDENTIFIERS
from kora.cel.tokens import Token, TokenKind, lex
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
    MapEntry,
    MapLiteral,
    Node,
    NullLiteral,
    Select,
    StringLiteral,
    UintLiteral,
    Unary,
    UnaryOperator,
)


@dataclass(frozen=True)
class ParseLimits:
    depth: int
    code_points: int | None


AUTHOR_LIMITS: Final[ParseLimits] = ParseLimits(MAX_DEPTH, MAX_CODE_POINTS)
RENDERING_LIMITS: Final[ParseLimits] = ParseLimits(MAX_RENDERING_DEPTH, None)

OR_LEVEL: Final[int] = 1

BINARY_LEVELS: Final[dict[TokenKind, tuple[int, BinaryOperator]]] = {
    TokenKind.OR: (1, BinaryOperator.OR),
    TokenKind.AND: (2, BinaryOperator.AND),
    TokenKind.EQUAL: (3, BinaryOperator.EQUAL),
    TokenKind.NOT_EQUAL: (3, BinaryOperator.NOT_EQUAL),
    TokenKind.LESS: (3, BinaryOperator.LESS),
    TokenKind.LESS_EQUAL: (3, BinaryOperator.LESS_EQUAL),
    TokenKind.GREATER: (3, BinaryOperator.GREATER),
    TokenKind.GREATER_EQUAL: (3, BinaryOperator.GREATER_EQUAL),
    TokenKind.IN: (3, BinaryOperator.IN),
    TokenKind.PLUS: (4, BinaryOperator.ADD),
    TokenKind.MINUS: (4, BinaryOperator.SUBTRACT),
    TokenKind.STAR: (5, BinaryOperator.MULTIPLY),
    TokenKind.SLASH: (5, BinaryOperator.DIVIDE),
    TokenKind.PERCENT: (5, BinaryOperator.MODULO),
}


MEMBER_SUFFIXES: Final[frozenset[TokenKind]] = frozenset(
    {TokenKind.DOT, TokenKind.LEFT_BRACKET}
)
FOLDABLE: Final[frozenset[TokenKind]] = frozenset({TokenKind.INT, TokenKind.DOUBLE})


@dataclass(frozen=True)
class Parsed:
    node: Node
    height: int


def is_name(node: Node) -> bool:
    while isinstance(node, Select):
        node = node.operand
    return isinstance(node, Identifier)


class Parser:
    def __init__(self, tokens: list[Token], limits: ParseLimits, meter: Meter) -> None:
        self.tokens: list[Token] = tokens
        self.limits: ParseLimits = limits
        self.position: int = 0
        self.meter: Meter = meter
        self.comprehensions: int = 0
        self.macro_depth: int = 0

    def current(self) -> Token:
        return self.tokens[self.position]

    def kind_ahead(self, ahead: int) -> TokenKind:
        index = min(self.position + ahead, len(self.tokens) - 1)
        return self.tokens[index].kind

    def advance(self) -> Token:
        token = self.tokens[self.position]
        if token.kind is not TokenKind.END:
            self.position += 1
        self.meter.tick()
        return token

    def expect(self, kind: TokenKind) -> Token:
        if self.current().kind is not kind:
            raise self.unexpected()
        return self.advance()

    def unexpected(self) -> DiagnosticError:
        token = self.current()
        if token.kind is TokenKind.END:
            return fail(Reason.UNEXPECTED_END, token.offset)
        return fail(Reason.UNEXPECTED_INPUT, token.offset)

    def descend(self, depth: int, offset: int) -> int:
        deeper = depth + 1
        if deeper > self.limits.depth:
            raise fail(Reason.DEPTH, offset)
        return deeper

    def built(self, node: Node, height: int, depth: int) -> Parsed:
        self.meter.tick()
        if depth + height > self.limits.depth:
            raise fail(Reason.DEPTH, node.offset)
        return Parsed(node, height)

    def parse(self) -> Node:
        result = self.expression(0)
        if self.current().kind is not TokenKind.END:
            raise self.unexpected()
        return result.node

    def expression(self, depth: int) -> Parsed:
        condition = self.binary(depth, OR_LEVEL)
        if self.current().kind is not TokenKind.QUESTION:
            return condition
        question = self.advance()
        inner = self.descend(depth, question.offset)
        then = self.binary(inner, OR_LEVEL)
        self.expect(TokenKind.COLON)
        otherwise = self.expression(inner)
        node = Conditional(condition.node, then.node, otherwise.node, question.offset)
        height = 1 + max(condition.height, then.height, otherwise.height)
        return self.built(node, height, depth)

    def binary(self, depth: int, minimum: int) -> Parsed:
        left = self.unary(depth)
        while True:
            entry = BINARY_LEVELS.get(self.current().kind)
            if entry is None or entry[0] < minimum:
                return left
            level, operator = entry
            token = self.advance()
            inner = self.descend(depth, token.offset)
            right = self.binary(inner, level + 1)
            node = Binary(operator, left.node, right.node, token.offset)
            left = self.built(node, 1 + max(left.height, right.height), depth)

    def unary(self, depth: int) -> Parsed:
        kind = self.current().kind
        if kind not in (TokenKind.BANG, TokenKind.MINUS):
            return self.member(depth)
        operators: list[Token] = []
        while self.current().kind is kind:
            operators.append(self.advance())
        literal_minus = self.foldable_minus()
        fold = kind is TokenKind.MINUS and len(operators) == 1 and self.foldable()
        levels = 0 if fold else len(operators)
        if depth + levels > self.limits.depth:
            raise fail(Reason.DEPTH, operators[self.limits.depth - depth].offset)
        if fold:
            operand = Parsed(self.negative_literal(operators[-1]), 0)
        elif kind is TokenKind.BANG and literal_minus:
            operand = Parsed(self.negative_literal(self.advance()), 0)
        else:
            operand = self.member(depth + levels)
        operator = UnaryOperator.NOT if kind is TokenKind.BANG else UnaryOperator.NEGATE
        node = operand.node
        height = operand.height
        for token in reversed(operators[:levels]):
            node = Unary(operator, node, token.offset)
            height += 1
            self.meter.tick()
        return Parsed(node, height)

    def foldable(self) -> bool:
        return (
            self.current().kind in FOLDABLE
            and self.kind_ahead(1) not in MEMBER_SUFFIXES
        )

    def foldable_minus(self) -> bool:
        return (
            self.current().kind is TokenKind.MINUS
            and self.kind_ahead(1) in FOLDABLE
            and self.kind_ahead(2) not in MEMBER_SUFFIXES
        )

    def negative_literal(self, minus: Token) -> Node:
        token = self.advance()
        if isinstance(token.value, int):
            value = -token.value
            if value < MIN_INT:
                raise fail(Reason.INVALID_LITERAL, minus.offset)
            return IntLiteral(value, minus.offset)
        if isinstance(token.value, float):
            return DoubleLiteral(-token.value, minus.offset)
        raise fail(Reason.INVALID_LITERAL, minus.offset)

    def member(self, depth: int) -> Parsed:
        result = self.primary(depth)
        while True:
            kind = self.current().kind
            if kind is TokenKind.DOT:
                self.advance()
                if self.current().kind is TokenKind.QUESTION:
                    raise fail(Reason.OPTIONAL_SYNTAX, self.current().offset)
                name = self.expect(TokenKind.IDENTIFIER)
                if self.current().kind is TokenKind.LEFT_PAREN:
                    result = self.receiver_call(depth, result, name)
                else:
                    node = Select(result.node, str(name.value), name.offset)
                    result = self.built(node, 1 + result.height, depth)
            elif kind is TokenKind.LEFT_BRACKET:
                bracket = self.advance()
                if self.current().kind is TokenKind.QUESTION:
                    raise fail(Reason.OPTIONAL_SYNTAX, self.current().offset)
                inner = self.descend(depth, bracket.offset)
                index = self.expression(inner)
                self.expect(TokenKind.RIGHT_BRACKET)
                node = Index(result.node, index.node, bracket.offset)
                result = self.built(node, 1 + max(result.height, index.height), depth)
            elif kind is TokenKind.LEFT_BRACE and is_name(result.node):
                raise fail(Reason.MESSAGE_CONSTRUCTION, self.current().offset)
            else:
                return result

    def arguments(self, depth: int) -> list[Parsed]:
        opening = self.expect(TokenKind.LEFT_PAREN)
        if self.current().kind is TokenKind.RIGHT_PAREN:
            self.advance()
            return []
        inner = self.descend(depth, opening.offset)
        items = [self.expression(inner)]
        while self.current().kind is TokenKind.COMMA:
            self.advance()
            items.append(self.expression(inner))
        self.expect(TokenKind.RIGHT_PAREN)
        return items

    def receiver_call(self, depth: int, receiver: Parsed, name: Token) -> Parsed:
        function_name = str(name.value)
        is_macro = function_name in MACRO_NAMES
        if is_macro:
            self.macro_depth += 1
        arguments = self.arguments(depth)
        if is_macro:
            self.macro_depth -= 1
            return self.comprehension(depth, receiver, name, arguments)
        if lookup(function_name, len(arguments), Form.RECEIVER) is None:
            raise fail(Reason.UNKNOWN_FUNCTION, name.offset)
        if function_name == "matches":
            self.check_pattern(arguments[0].node)
        node = Call(
            function_name,
            receiver.node,
            tuple(argument.node for argument in arguments),
            name.offset,
        )
        height = 1 + max(
            [receiver.height, *(argument.height for argument in arguments)]
        )
        return self.built(node, height, depth)

    def comprehension(
        self, depth: int, receiver: Parsed, name: Token, arguments: list[Parsed]
    ) -> Parsed:
        macro = Macro(str(name.value))
        count = len(arguments)
        if macro is not Macro.MAP and count == 3:
            raise fail(Reason.TWO_VARIABLE_COMPREHENSION, name.offset)
        allowed = (2, 3) if macro is Macro.MAP else (2,)
        if count not in allowed:
            raise fail(Reason.MALFORMED_MACRO, name.offset)
        variable = arguments[0].node
        if not isinstance(variable, Identifier):
            raise fail(Reason.MALFORMED_MACRO, variable.offset)
        if self.macro_depth + 1 > MAX_COMPREHENSION_NESTING:
            raise fail(Reason.COMPREHENSION_NESTING, name.offset)
        self.comprehensions += 1
        if self.comprehensions > MAX_COMPREHENSIONS:
            raise fail(Reason.COMPREHENSION_COUNT, name.offset)
        body = arguments[1:]
        node = Comprehension(
            macro,
            receiver.node,
            variable.name,
            tuple(part.node for part in body),
            name.offset,
        )
        height = 1 + max([receiver.height, *(part.height for part in body)])
        return self.built(node, height, depth)

    def check_pattern(self, argument: Node) -> None:
        if not isinstance(argument, StringLiteral):
            raise fail(Reason.NON_LITERAL_PATTERN, argument.offset)
        try:
            parse_pattern(argument.value, self.meter)
        except PatternError as error:
            raise fail(error.reason, argument.offset) from None

    def global_call(self, depth: int, name: Token) -> Parsed:
        function_name = str(name.value)
        arguments = self.arguments(depth)
        if function_name == "has":
            target = arguments[0].node if len(arguments) == 1 else None
            if not isinstance(target, Select):
                raise fail(Reason.MALFORMED_MACRO, name.offset)
            return self.built(Has(target, name.offset), 1 + arguments[0].height, depth)
        if lookup(function_name, len(arguments), Form.GLOBAL) is None:
            raise fail(Reason.UNKNOWN_FUNCTION, name.offset)
        node = Call(
            function_name,
            None,
            tuple(argument.node for argument in arguments),
            name.offset,
        )
        height = 1 + max(argument.height for argument in arguments)
        return self.built(node, height, depth)

    def primary(self, depth: int) -> Parsed:
        token = self.current()
        kind = token.kind
        if kind is TokenKind.IDENTIFIER:
            self.advance()
            name = str(token.value)
            if name in RESERVED_IDENTIFIERS:
                raise fail(Reason.RESERVED_IDENTIFIER, token.offset)
            if self.current().kind is TokenKind.LEFT_PAREN:
                return self.global_call(depth, token)
            return Parsed(Identifier(name, token.offset), 0)
        if kind is TokenKind.DOT:
            raise fail(Reason.QUALIFIED_NAME, token.offset)
        if kind is TokenKind.LEFT_PAREN:
            self.advance()
            inner = self.descend(depth, token.offset)
            result = self.expression(inner)
            self.expect(TokenKind.RIGHT_PAREN)
            return Parsed(result.node, result.height + 1)
        if kind is TokenKind.LEFT_BRACKET:
            return self.list_literal(depth)
        if kind is TokenKind.LEFT_BRACE:
            return self.map_literal(depth)
        return Parsed(self.literal(), 0)

    def literal(self) -> Node:
        token = self.current()
        value = token.value
        kind = token.kind
        if kind is TokenKind.INT and isinstance(value, int):
            if value > MAX_INT:
                raise fail(Reason.INVALID_LITERAL, token.offset)
            node: Node = IntLiteral(value, token.offset)
        elif kind is TokenKind.UINT and isinstance(value, int):
            if value > MAX_UINT:
                raise fail(Reason.INVALID_LITERAL, token.offset)
            node = UintLiteral(value, token.offset)
        elif kind is TokenKind.DOUBLE and isinstance(value, float):
            node = DoubleLiteral(value, token.offset)
        elif kind is TokenKind.STRING and isinstance(value, str):
            node = StringLiteral(value, token.offset)
        elif kind is TokenKind.BYTES and isinstance(value, bytes):
            node = BytesLiteral(value, token.offset)
        elif kind is TokenKind.TRUE:
            node = BoolLiteral(True, token.offset)
        elif kind is TokenKind.FALSE:
            node = BoolLiteral(False, token.offset)
        elif kind is TokenKind.NULL:
            node = NullLiteral(token.offset)
        else:
            raise self.unexpected()
        self.advance()
        return node

    def list_literal(self, depth: int) -> Parsed:
        opening = self.advance()
        elements: list[Parsed] = []
        inner = depth
        while self.current().kind is not TokenKind.RIGHT_BRACKET:
            if self.current().kind is TokenKind.QUESTION:
                raise fail(Reason.OPTIONAL_SYNTAX, self.current().offset)
            if not elements:
                inner = self.descend(depth, opening.offset)
            elements.append(self.expression(inner))
            if self.current().kind is not TokenKind.COMMA:
                break
            self.advance()
        self.expect(TokenKind.RIGHT_BRACKET)
        node = ListLiteral(tuple(element.node for element in elements), opening.offset)
        height = 1 + max(element.height for element in elements) if elements else 0
        return self.built(node, height, depth)

    def map_literal(self, depth: int) -> Parsed:
        opening = self.advance()
        entries: list[MapEntry] = []
        heights: list[int] = []
        inner = depth
        while self.current().kind is not TokenKind.RIGHT_BRACE:
            if self.current().kind is TokenKind.QUESTION:
                raise fail(Reason.OPTIONAL_SYNTAX, self.current().offset)
            if not entries:
                inner = self.descend(depth, opening.offset)
            key = self.expression(inner)
            self.expect(TokenKind.COLON)
            value = self.expression(inner)
            entries.append(MapEntry(key.node, value.node))
            heights.extend((key.height, value.height))
            if self.current().kind is not TokenKind.COMMA:
                break
            self.advance()
        self.expect(TokenKind.RIGHT_BRACE)
        node = MapLiteral(tuple(entries), opening.offset)
        height = 1 + max(heights) if heights else 0
        return self.built(node, height, depth)


def parse(
    text: str, limits: ParseLimits = AUTHOR_LIMITS, meter: Meter | None = None
) -> Node:
    if limits.code_points is not None and len(text) > limits.code_points:
        raise fail(Reason.LENGTH, limits.code_points)
    used = meter if meter is not None else Meter()
    tokens = lex(text, used)
    return Parser(tokens, limits, used).parse()
