import hashlib
import json
from pathlib import Path

import pytest

from kora.ir.json import parse_json_object
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.measurements.storage_evidence_fixture import write_junit
from tests.scaffold_evidence import (
    COHORTS,
    ENVIRONMENTS,
    GENERAL_ARGUMENTS,
    LANES,
    SHARD_CHILDREN,
    STORAGE_ARGUMENTS,
    collection_nodes,
    record_lane,
    verify_scaffold,
)

REVISION = "a" * 40
REQUESTED = "b" * 40


GENERAL_NODES = (
    "tests/test_general.py::TestCase::test_value[sqlite-a::b/c]",
    "tests/test_second.py::test_value",
    "tests/measurements/test_value.py::test_value",
    "tests/db/test_value.py::test_value[sqlite]",
    "tests/test_other.py::test_value",
)
STORAGE_NODES = ("tests/plugins/storage/test_value.py::test_value[sqlite]",)


def collection_text(nodes: tuple[str, ...]) -> str:
    return "\n".join(nodes) + f"\n\n{len(nodes)} tests collected in 0.01s\n"


def lane_files(root: Path, lane: str) -> None:
    root.mkdir(parents=True)
    selected = STORAGE_NODES if lane == "storage" else GENERAL_NODES
    (root / "selected.txt").write_text(collection_text(selected))
    (root / "selected.stderr").write_text("")
    (root / "collection.exit").write_text("0\n")
    (root / "tests.exit").write_text("0\n")
    (root / "tests.stdout").write_text("1 passed\n")
    (root / "tests.stderr").write_text("")
    if lane == "conformance":
        (root / "complete.txt").write_text(
            "\n".join((*GENERAL_NODES, *STORAGE_NODES)) + "\n"
        )
        (root / "complete.stderr").write_text("")
        (root / "complete.exit").write_text("0\n")
    if lane in SHARD_CHILDREN:
        (root / "full.collection").write_text(collection_text(selected))
        for name, node in zip(COHORTS, selected, strict=True):
            (root / f"{name}.collection").write_text(collection_text((node,)))
        for name in SHARD_CHILDREN[lane]:
            write_junit(
                root / f"{name}.xml", collection_nodes(root / f"{name}.collection")
            )
            (root / f"{name}.log").write_text("1 passed\n")
            (root / f"{name}.exit").write_text("0\n")
        (root / "inventory.json").write_text(
            '{"full":5,"first":1,"second":1,"measurements":1,"integration":1,"other":1}'
        )
    else:
        write_junit(root / "junit.xml", collection_nodes(root / "selected.txt"))


def complete_evidence(root: Path) -> None:
    for operating_system, python, databases in ENVIRONMENTS:
        for lane in LANES:
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
                STORAGE_ARGUMENTS if lane == "storage" else GENERAL_ARGUMENTS,
            )


def rehash(directory: Path, changed: list[str]) -> None:
    receipt = directory / "receipt.json"
    data = parse_json_object(json.loads(receipt.read_text()))
    files = parse_json_object(data["files"])
    for name in changed:
        files[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    data["files"] = files
    receipt.write_text(json.dumps(data))


def test_scaffold_receipts_prove_complete_disjoint_execution(tmp_path: Path) -> None:
    complete_evidence(tmp_path)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)

    assert result == {"environments": 3, "lanes": 9, "tests": 18}


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing", "artifact has unexpected file coverage"),
        ("failed", "execution did not succeed"),
        ("skipped", "test did not pass"),
        ("mismatch", "ordinary JUnit differs"),
        ("overlap", "ordinary cohorts overlap"),
        ("full", "full collection differs"),
    ],
)
def test_general_cohort_evidence_refuses_rehashed_semantic_corruption(
    tmp_path: Path, corruption: str, match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-macos-15-3.11.17-conformance"
    changed: list[str] = []
    if corruption == "missing":
        (directory / "second.xml").unlink()
    elif corruption == "failed":
        (directory / "second.exit").write_text("1\n")
        changed.append("second.exit")
    elif corruption == "skipped":
        (directory / "second.xml").write_text(
            '<testsuite><testcase classname="tests.test_second" name="test_value">'
            "<skipped/></testcase></testsuite>"
        )
        changed.append("second.xml")
    elif corruption == "mismatch":
        write_junit(directory / "second.xml", frozenset({"tests.wrong.test_value"}))
        changed.append("second.xml")
    elif corruption == "overlap":
        (directory / "second.collection").write_bytes(
            (directory / "first.collection").read_bytes()
        )
        (directory / "second.xml").write_bytes((directory / "first.xml").read_bytes())
        changed.extend(("second.collection", "second.xml"))
    else:
        (directory / "full.collection").write_bytes(
            (directory / "first.collection").read_bytes()
        )
        changed.append("full.collection")
    rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


@pytest.mark.parametrize(
    ("corruption", "failure", "match"),
    [
        ("missing", FileNotFoundError, "receipt.json"),
        ("extra", EvidenceFailure, "exactly nine lanes"),
        ("stale", EvidenceFailure, "mismatched provenance"),
        ("hash", EvidenceFailure, "hash differs from its receipt"),
        ("failed", EvidenceFailure, "execution did not succeed"),
        ("skipped", EvidenceFailure, "test did not pass"),
        ("execution", EvidenceFailure, "JUnit differs"),
        ("union", EvidenceFailure, "do not partition"),
        ("overlap", EvidenceFailure, "do not partition"),
    ],
)
def test_scaffold_evidence_refuses_incomplete_or_mismatched_execution(
    tmp_path: Path, corruption: str, failure: type[Exception], match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-macos-15-3.11.17-storage"
    if corruption == "union":
        directory = tmp_path / "scaffold-tests-macos-15-3.11.17-conformance"
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
            replacement = path.read_text() + "tests/test_unexecuted.py::test_value\n"
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

    with pytest.raises(failure, match=match):
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


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing lane", "exactly nine lanes"),
        ("unequal inventories", "shards differ"),
        ("different cohorts", "shards differ"),
        ("wrong shard", "unexpected file coverage"),
        ("remainder junit", "JUnit differs"),
    ],
)
def test_scaffold_evidence_requires_both_general_shards_to_agree(
    tmp_path: Path, corruption: str, match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-ubuntu-24.04-3.15.0-remainder"
    changed: list[str] = []
    if corruption == "missing lane":
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    elif corruption == "unequal inventories":
        (directory / "inventory.json").write_text('{"full":5}')
        changed.append("inventory.json")
    elif corruption == "different cohorts":
        (directory / "other.collection").write_text(
            collection_text((GENERAL_NODES[3],))
        )
        (directory / "integration.collection").write_text(
            collection_text((GENERAL_NODES[4],))
        )
        write_junit(
            directory / "other.xml", collection_nodes(directory / "other.collection")
        )
        write_junit(
            directory / "integration.xml",
            collection_nodes(directory / "integration.collection"),
        )
        changed.extend(
            (
                "other.collection",
                "integration.collection",
                "other.xml",
                "integration.xml",
            )
        )
    elif corruption == "wrong shard":
        (directory / "first.log").write_text("1 passed\n")
    else:
        write_junit(
            directory / "integration.xml", frozenset({"tests.wrong.test_value"})
        )
        changed.append("integration.xml")
    if directory.exists():
        rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)
