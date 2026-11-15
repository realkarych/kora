from dataclasses import dataclass, field
from fractions import Fraction

View = None | bool | int | Fraction | str | list["View"] | dict[str, "View"]


def numeric_score(bits: int) -> int:
    return 16 * ((2 * bits + 31) // 30) ** 2


def integer_repr(bits: int) -> int:
    return 2 + (bits * 30103 + 99999) // 100000


@dataclass(slots=True)
class Bounds:
    nodes: int = 0
    encoded: int = 0
    key_bytes: int = 0
    bits: int = 0
    numeric: bool = False

    def include(self, child: "Bounds") -> None:
        self.nodes += child.nodes
        self.encoded += child.encoded
        self.key_bytes = max(self.key_bytes, child.key_bytes)
        self.bits = max(self.bits, child.bits)
        self.numeric |= child.numeric

    def maximum(self, child: "Bounds") -> None:
        self.nodes = max(self.nodes, child.nodes)
        self.encoded = max(self.encoded, child.encoded)
        self.key_bytes = max(self.key_bytes, child.key_bytes)
        self.bits = max(self.bits, child.bits)
        self.numeric |= child.numeric


@dataclass(slots=True)
class Statistics:
    tree: Bounds = field(default_factory=Bounds)
    element: Bounds = field(default_factory=Bounds)
    depth: int = 0
    array: int = 0
    members: int = 0
    representations: int = 0
    owned_bytes: int = 0
    conversions: int = 0
    positions: int = 0
    references: int = 0
