from typing import Final

REJECTED_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0000, 0x0008),
    (0x000B, 0x000B),
    (0x000E, 0x001F),
    (0x007F, 0x009F),
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x206F),
    (0xD800, 0xDFFF),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

REJECTED: Final[frozenset[int]] = frozenset(
    point for low, high in REJECTED_RANGES for point in range(low, high + 1)
)
WHITESPACE: Final[frozenset[str]] = frozenset(" \t\n\f\r")
NEWLINES: Final[frozenset[str]] = frozenset("\n\r")
COMMENT_END: Final[str] = "\n"
DIGITS: Final[frozenset[str]] = frozenset("0123456789")
HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")
OCTAL_DIGITS: Final[frozenset[str]] = frozenset("01234567")
IDENTIFIER_START: Final[frozenset[str]] = frozenset(
    "_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
IDENTIFIER_PART: Final[frozenset[str]] = IDENTIFIER_START | DIGITS
PRINTABLE_ASCII: Final[frozenset[int]] = frozenset(range(0x20, 0x7F))


def is_rejected(character: str) -> bool:
    return ord(character) in REJECTED
