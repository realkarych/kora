from collections.abc import Iterator
from dataclasses import dataclass

from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.type_declarations import PUBLISHED_BUILTINS


@dataclass(frozen=True)
class TypeAncestry:
    intervals: dict[str, tuple[int, int]]

    def accepts(self, produced: str, expected: str) -> bool | None:
        child = self.intervals.get(produced)
        ancestor = self.intervals.get(expected)
        if child is None or ancestor is None:
            return None
        return ancestor[0] <= child[0] < ancestor[1]


def index_type_ancestry(contracts: dict[str, TypeContract]) -> TypeAncestry:
    roots: list[str] = []
    children: dict[str, list[str]] = {}
    for name, contract in contracts.items():
        if isinstance(contract, BuiltinTypeContract):
            if name in PUBLISHED_BUILTINS and contract.builtin_revision == "1":
                roots.append(name)
        elif "/" in name:
            if contract.parent is None:
                roots.append(name)
            else:
                children.setdefault(contract.parent, []).append(name)
    intervals: dict[str, tuple[int, int]] = {}
    position = 0
    for root in roots:
        pending: list[tuple[str, int, Iterator[str]]] = [
            (root, position, iter(children.get(root, ())))
        ]
        position += 1
        while pending:
            name, start, descendants = pending[-1]
            child = next(descendants, None)
            if child is None:
                intervals[name] = (start, position)
                pending.pop()
            else:
                pending.append((child, position, iter(children.get(child, ()))))
                position += 1
    return TypeAncestry(intervals)
