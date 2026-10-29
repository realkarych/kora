from pathlib import Path

import pytest

from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.scaffold_evidence import collection


@pytest.mark.parametrize("deselected", [0, 380])
def test_ordinary_collection_accepts_complete_selected_count(
    tmp_path: Path, deselected: int
) -> None:
    nodes = tuple(f"tests/example.py::test_value[{index}]" for index in range(5))
    suffix = (
        "/385 tests collected (380 deselected)" if deselected else " tests collected"
    )
    path = tmp_path / "collection"
    path.write_text("\n".join(nodes) + f"\n\n5{suffix} in 1.26s\n")

    result = collection(path)

    assert result == frozenset(nodes)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tests/example.py::test_value\n",
        "1 test collected in 0.01s\n",
        "tests/example.py::test_value\n0 tests collected in 0.01s\n",
        "tests/example.py::test_value\n2 tests collected in 0.01s\n",
        "tests/example.py::test_value\n1/3 tests collected (1 deselected) in 0.01s\n",
        "tests/example.py::test_value\n1 tests collected (1 deselected) in 0.01s\n",
        "tests/example.py::test_value\n1/3 tests collected in 0.01s\n",
        "tests/example.py::test_value\n1 test collected in 0.01s\nwarning\n",
        "warning\ntests/example.py::test_value\n1 test collected in 0.01s\n",
        "tests/example.py::test_value\ntests/example.py::test_value\n"
        "2 tests collected in 0.01s\n",
        "outside/example.py::test_value\n1 test collected in 0.01s\n",
        "tests/example.py\n1 test collected in 0.01s\n",
        "tests/example.py::test_value\n1 test collected in 0.01s partial\n",
    ],
)
def test_ordinary_collection_rejects_incomplete_or_malformed_evidence(
    tmp_path: Path, text: str
) -> None:
    source = tmp_path / "collection"
    source.write_text(text)

    with pytest.raises(ValueError, match="ordinary collection"):
        collection(source)


@pytest.mark.parametrize("case", ["oversized", "symlink"])
def test_ordinary_collection_requires_bounded_regular_input(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "collection"
    if case == "oversized":
        with path.open("wb") as stream:
            stream.truncate(16777217)
    else:
        target = tmp_path / "target"
        target.write_text("tests/example.py::test_value\n1 test collected in 0.01s\n")
        path.symlink_to(target)

    with pytest.raises(EvidenceFailure, match="bounded regular"):
        collection(path)
