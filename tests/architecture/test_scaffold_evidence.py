import hashlib
import json
from pathlib import Path

import pytest

from kora.ir.json import parse_json_object
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.scaffold_evidence import (
    ENVIRONMENTS,
    GENERAL_ARGUMENTS,
    STORAGE_ARGUMENTS,
    collection_nodes,
    record_lane,
    verify_scaffold,
)

REVISION = "a" * 40
REQUESTED = "b" * 40


def lane_files(root: Path, lane: str) -> None:
    root.mkdir(parents=True)
    nodes = {
        "general": "tests/test_general.py::TestCase::test_value[sqlite-a::b/c]",
        "storage": "tests/plugins/storage/test_value.py::test_value[sqlite]",
    }
    selected = nodes[lane]
    (root / "selected.txt").write_text(f"{selected}\n\n1 test collected in 0.01s\n")
    (root / "selected.stderr").write_text("")
    (root / "collection.exit").write_text("0\n")
    (root / "tests.exit").write_text("0\n")
    (root / "tests.stdout").write_text("1 passed\n")
    (root / "tests.stderr").write_text("")
    identity = next(iter(collection_nodes(root / "selected.txt")))
    classname, _, name = identity.partition(".test_value")
    (root / "junit.xml").write_text(
        f'<testsuites><testsuite><testcase classname="{classname}" '
        f'name="test_value{name}"/></testsuite></testsuites>'
    )
    if lane == "general":
        (root / "complete.txt").write_text("\n".join(nodes.values()) + "\n")
        (root / "complete.stderr").write_text("")
        (root / "complete.exit").write_text("0\n")


def complete_evidence(root: Path) -> None:
    for operating_system, python, databases in ENVIRONMENTS:
        for lane in ("general", "storage"):
            directory = root / f"scaffold-tests-{operating_system}-{python}-{lane}"
            lane_files(directory, lane)
            record_lane(
                directory,
                "123",
                "1",
                REVISION,
                REQUESTED,
                operating_system,
                python,
                databases,
                lane,
                GENERAL_ARGUMENTS if lane == "general" else STORAGE_ARGUMENTS,
            )


def test_scaffold_receipts_prove_complete_disjoint_execution(tmp_path: Path) -> None:
    complete_evidence(tmp_path)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)

    assert result == {"environments": 3, "lanes": 6, "tests": 6}


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "extra",
        "stale",
        "hash",
        "failed",
        "skipped",
        "execution",
        "union",
        "overlap",
    ],
)
def test_scaffold_evidence_refuses_incomplete_or_mismatched_execution(
    tmp_path: Path, corruption: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-macos-15-3.11.17-storage"
    if corruption == "union":
        directory = tmp_path / "scaffold-tests-macos-15-3.11.17-general"
    receipt = directory / "receipt.json"
    if corruption == "missing":
        receipt.unlink()
    elif corruption == "extra":
        (tmp_path / "unexpected").mkdir()
    elif corruption == "stale":
        data = parse_json_object(json.loads(receipt.read_text()))
        data["run_id"] = "122"
        receipt.write_text(json.dumps(data))
    else:
        target = {
            "hash": "tests.stdout",
            "failed": "tests.exit",
            "skipped": "junit.xml",
            "execution": "selected.txt",
            "union": "complete.txt",
            "overlap": "selected.txt",
        }[corruption]
        path = directory / target
        if corruption == "union":
            replacement = path.read_text() + "tests/test_other.py::test_value\n"
        elif corruption == "overlap":
            replacement = (
                path.read_text()
                + "tests/test_general.py::TestCase::test_value[sqlite-a::b/c]\n"
            )
        else:
            replacement = {
                "hash": "changed",
                "failed": "1\n",
                "execution": "tests/test_other.py::test_value\n",
                "skipped": (
                    '<testsuites><testsuite><testcase classname="a" name="b">'
                    "<skipped/></testcase></testsuite></testsuites>"
                ),
            }[corruption]
        path.write_text(replacement)
        changed = [target]
        if corruption == "overlap":
            junit = directory / "junit.xml"
            testcase = (
                '<testcase classname="tests.test_general.TestCase" '
                'name="test_value[sqlite-a::b/c]"/>'
            )
            junit.write_text(
                junit.read_text().replace("</testsuite>", testcase + "</testsuite>")
            )
            changed.append("junit.xml")
        if corruption != "hash":
            data = parse_json_object(json.loads(receipt.read_text()))
            files = parse_json_object(data["files"])
            for name in changed:
                files[name] = hashlib.sha256(
                    (directory / name).read_bytes()
                ).hexdigest()
            data["files"] = files
            receipt.write_text(json.dumps(data))

    if corruption in {"union", "overlap"}:
        match = "do not partition"
    elif corruption == "execution":
        match = "JUnit differs"
    else:
        match = None

    with pytest.raises((EvidenceFailure, FileNotFoundError), match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


@pytest.mark.parametrize(
    "content",
    ["", "tests/a.py::test_a\ntests/a.py::test_a\n", "ERROR collecting tests/a.py\n"],
)
def test_collection_requires_a_nonempty_unique_successful_inventory(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "selected.txt"
    path.write_text(content)

    with pytest.raises(EvidenceFailure):
        collection_nodes(path)


def test_collection_preserves_parameter_identity(tmp_path: Path) -> None:
    path = tmp_path / "selected.txt"
    path.write_text(
        "tests/test_general.py::TestCase::test_value[sqlite-a::b/c]\n"
        "\n1/2 tests collected (1 deselected) in 0.01s\n"
    )

    result = collection_nodes(path)

    assert result == {"tests.test_general.TestCase.test_value[sqlite-a::b/c]"}
