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
    MapEntry,
    MapLiteral,
    Node,
    NullLiteral,
    Select,
    StringLiteral,
    UintLiteral,
    Unary,
)


class NegationRewriter:
    def __init__(self, negations: frozenset[int], meter: Meter) -> None:
        self.negations: frozenset[int] = negations
        self.meter: Meter = meter

    def nodes(self, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        return tuple(self.node(node) for node in nodes)

    def select(self, node: Select) -> Select:
        return Select(self.node(node.operand), node.name, node.offset)

    def unary(self, node: Unary) -> Node:
        operand = self.node(node.operand)
        if id(node) in self.negations:
            zero = IntLiteral(0, node.offset)
            return Binary(BinaryOperator.SUBTRACT, zero, operand, node.offset)
        return Unary(node.operator, operand, node.offset)

    def map_literal(self, node: MapLiteral) -> MapLiteral:
        entries = tuple(
            MapEntry(self.node(entry.key), self.node(entry.value))
            for entry in node.entries
        )
        return MapLiteral(entries, node.offset)

    def node(self, node: Node) -> Node:
        self.meter.tick()
        match node:
            case Unary():
                return self.unary(node)
            case Select():
                return self.select(node)
            case Binary(operator=operator, left=left, right=right, offset=offset):
                return Binary(operator, self.node(left), self.node(right), offset)
            case Index(operand=operand, index=index, offset=offset):
                return Index(self.node(operand), self.node(index), offset)
            case Call(
                function=function, receiver=receiver, arguments=arguments, offset=offset
            ):
                target = None if receiver is None else self.node(receiver)
                return Call(function, target, self.nodes(arguments), offset)
            case ListLiteral(elements=elements, offset=offset):
                return ListLiteral(self.nodes(elements), offset)
            case MapLiteral():
                return self.map_literal(node)
            case Conditional(
                condition=condition, then=then, otherwise=otherwise, offset=offset
            ):
                return Conditional(
                    self.node(condition), self.node(then), self.node(otherwise), offset
                )
            case Has(target=target, offset=offset):
                return Has(self.select(target), offset)
            case Comprehension(
                macro=macro,
                range=range_,
                variable=variable,
                arguments=arguments,
                offset=offset,
            ):
                return Comprehension(
                    macro, self.node(range_), variable, self.nodes(arguments), offset
                )
            case (
                IntLiteral()
                | UintLiteral()
                | DoubleLiteral()
                | StringLiteral()
                | BytesLiteral()
                | BoolLiteral()
                | NullLiteral()
                | Identifier()
            ):
                return node


def rewrite_integer_negations(
    tree: Node, negations: frozenset[int], meter: Meter
) -> Node:
    if not negations:
        return tree
    return NegationRewriter(negations, meter).node(tree)
