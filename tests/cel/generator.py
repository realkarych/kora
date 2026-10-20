from collections.abc import Sequence
from dataclasses import dataclass

STRING_PIECES: tuple[str, ...] = (
    "a",
    "Z",
    " ",
    "\\n",
    "\\t",
    "\\\\",
    "\\x41",
    "\\101",
    "\\u00e9",
    "\\U0001f600",
    "\u00e9",
    "\U0001f600",
    "\u4e2d",
)
IDENTIFIERS: tuple[str, ...] = ("a", "b", "inputs", "response", "x_1")
FIELDS: tuple[str, ...] = ("b", "decision", "exit_code", "as", "if")
BINARY: tuple[str, ...] = (
    "+",
    "-",
    "*",
    "/",
    "%",
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "&&",
    "||",
    " in ",
)
SPACES: tuple[str, ...] = ("", " ", "  ", "\t", "\n", " // c\n")
DOUBLES: tuple[str, ...] = ("1.5", "0.0", "-0.0", "1e10", ".5", "2E-3", "-7.25")
MACROS: tuple[str, ...] = ("all", "exists", "exists_one", "filter", "map")
FUNCTIONS: tuple[str, ...] = ("size", "string", "int", "double", "dyn")
METHODS: tuple[str, ...] = ("contains", "startsWith", "endsWith")


MODULUS: int = 2**64
MULTIPLIER: int = 6364136223846793005
INCREMENT: int = 1442695040888963407


@dataclass
class SeededSource:
    state: int

    def next(self) -> int:
        self.state = (self.state * MULTIPLIER + INCREMENT) % MODULUS
        return self.state >> 16

    def randrange(self, start: int, stop: int | None = None) -> int:
        low, high = (0, start) if stop is None else (start, stop)
        return low + self.next() % (high - low)

    def random(self) -> float:
        return (self.next() % 1_000_000) / 1_000_000

    def choice(self, options: Sequence[str]) -> str:
        return options[self.randrange(len(options))]


@dataclass(frozen=True)
class Fragment:
    text: str
    compound: bool


@dataclass
class ExpressionGenerator:
    random: SeededSource
    macros: int = 0

    def space(self) -> str:
        return self.random.choice(SPACES)

    def string_literal(self) -> str:
        quote = self.random.choice(("'", '"'))
        pieces = [self.random.choice(STRING_PIECES) for _ in range(4)]
        if self.random.random() < 0.3:
            pieces.append("\\" + quote)
        return quote + "".join(pieces) + quote

    def literal(self) -> str:
        choice = self.random.randrange(9)
        if choice == 0:
            return str(self.random.randrange(0, 1000))
        if choice == 1:
            return f"{self.random.randrange(1000)}u"
        if choice == 2:
            return self.random.choice(DOUBLES).lstrip("-")
        if choice == 3:
            return self.string_literal()
        if choice == 4:
            return "b'" + self.random.choice(("ab", "\\xff", "\\000", "\u00e9")) + "'"
        if choice == 5:
            return self.random.choice(("true", "false", "null"))
        if choice == 6:
            return "r'\\d+'"
        if choice == 7:
            return "0x" + format(self.random.randrange(4096), "x")
        return "'''line\nnext'''"

    def operand(self, fragment: Fragment) -> str:
        if fragment.compound or self.random.random() < 0.1:
            return f"({self.space()}{fragment.text}{self.space()})"
        return fragment.text

    def member(self, depth: int) -> str:
        base = self.random.choice(IDENTIFIERS)
        for _ in range(self.random.randrange(3)):
            if self.random.random() < 0.7:
                base += "." + self.random.choice(FIELDS)
            else:
                base += "[" + self.expression(depth + 1).text + "]"
        return base

    def leaf(self, depth: int) -> Fragment:
        if self.random.random() < 0.5:
            return Fragment(self.literal(), False)
        return Fragment(self.member(depth), False)

    def expression(self, depth: int) -> Fragment:
        if depth >= 5:
            return self.leaf(depth)
        inner = depth + 1
        choice = self.random.randrange(12)
        s = self.space
        if choice == 0:
            left = self.operand(self.expression(inner))
            right = self.operand(self.expression(inner))
            operator = self.random.choice(BINARY)
            return Fragment(f"{left}{s()}{operator}{s()}{right}", True)
        if choice == 1:
            sign = self.random.choice(("!", "-", "--"))
            if sign != "!" and self.random.random() < 0.5:
                number = self.random.choice((*DOUBLES[:2], "9223372036854775808", "7"))
                return Fragment(f"-{number.lstrip('-')}", True)
            return Fragment(f"{sign}{self.member(inner)}", True)
        if choice == 2:
            parts = [self.operand(self.expression(inner)) for _ in range(3)]
            return Fragment(
                f"{parts[0]}{s()}?{s()}{parts[1]}{s()}:{s()}{parts[2]}", True
            )
        if choice == 3:
            return Fragment(f"({s()}{self.expression(inner).text}{s()})", False)
        if choice == 4:
            count = self.random.randrange(4)
            items = ", ".join(self.expression(inner).text for _ in range(count))
            return Fragment(f"[{items}]", False)
        if choice == 5:
            count = self.random.randrange(3)
            entries = ", ".join(
                f"{self.literal()}: {self.expression(inner).text}" for _ in range(count)
            )
            return Fragment("{" + entries + "}", False)
        if choice == 6:
            field = self.random.choice(FIELDS)
            return Fragment(f"has({self.member(inner)}.{field})", False)
        if choice == 7 and self.macros < 2 and depth < 2:
            self.macros += 1
            macro = self.random.choice(MACROS)
            body = self.expression(inner).text
            return Fragment(f"{self.member(inner)}.{macro}(v, {body})", False)
        if choice == 8:
            function = self.random.choice(FUNCTIONS)
            return Fragment(f"{function}({self.expression(inner).text})", False)
        if choice == 9:
            method = self.random.choice(METHODS)
            return Fragment(f"{self.member(inner)}.{method}({self.literal()})", False)
        if choice == 10:
            pattern = "'^[a-z]+(?:-\\\\d{1,3})?$'"
            return Fragment(f"{self.member(inner)}.matches({pattern})", False)
        return self.leaf(depth)


def generated_expressions(seed: int, count: int) -> list[str]:
    source = SeededSource(seed)
    return [ExpressionGenerator(source).expression(0).text for _ in range(count)]
