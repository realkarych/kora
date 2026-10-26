import json
import re
import sys
from pathlib import Path


def collection(path: Path) -> frozenset[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
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
    first = collection(root / "first.collection")
    second = collection(root / "second.collection")
    other = collection(root / "other.collection")
    if first & second or first & other or second & other:
        raise ValueError("overlapping ordinary collections")
    if full != first | second | other:
        raise ValueError("incomplete ordinary collection union")
    return {
        "full": len(full),
        "first": len(first),
        "second": len(second),
        "other": len(other),
    }


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise ValueError("expected ordinary evidence directory")
    result = inventory(Path(arguments[0]))
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
