from typing import Final

RESERVED_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        "as",
        "break",
        "const",
        "continue",
        "else",
        "for",
        "function",
        "if",
        "import",
        "let",
        "loop",
        "namespace",
        "package",
        "return",
        "var",
        "void",
        "while",
    }
)
