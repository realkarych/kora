import json
import re
import sys
from pathlib import Path

from tests.scaffold_evidence import COHORTS, evidence_bytes


def collection(path: Path) -> frozenset[str]:
    if not path.is_file():
        raise ValueError("missing ordinary collection")
    lines = [line for line in evidence_bytes(path).decode("utf-8").splitlines() if line]
    if not lines:
        raise ValueError("empty ordinary collection")
    summary = re.fullmatch(
        r"([1-9][0-9]*)(?:/([1-9][0-9]*))? tests? collected"
        r"(?: \(([1-9][0-9]*) deselected\))? in [0-9]+\.[0-9]+s",
        lines[-1],
    )
    if summary is None:
        raise ValueError("incomplete ordinary collection summary")
    selected = int(summary[1])
    total = int(summary[2]) if summary[2] is not None else selected
    deselected = int(summary[3]) if summary[3] is not None else 0
    nodes = lines[:-1]
    if (
        total != selected + deselected
        or (summary[2] is None) != (summary[3] is None)
        or len(nodes) != selected
        or any(
            re.fullmatch(r"tests/[^\r\n]+\.py::[^\r\n]+", node) is None
            for node in nodes
        )
        or len(frozenset(nodes)) != selected
    ):
        raise ValueError("invalid ordinary collection inventory")
    return frozenset(nodes)


def inventory(root: Path) -> dict[str, int]:
    full = collection(root / "full.collection")
    groups = {name: collection(root / f"{name}.collection") for name in COHORTS}
    union = frozenset[str]().union(*groups.values())
    if sum(len(nodes) for nodes in groups.values()) != len(union):
        raise ValueError("overlapping ordinary collections")
    if full != union:
        raise ValueError("incomplete ordinary collection union")
    return {"full": len(full)} | {name: len(nodes) for name, nodes in groups.items()}


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise ValueError("expected ordinary evidence directory")
    result = inventory(Path(arguments[0]))
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
