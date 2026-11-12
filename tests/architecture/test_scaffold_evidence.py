import hashlib
import json
import subprocess
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError

import pytest
from pydantic import ValidationError

from kora.ir.json import parse_json_object
from scripts.ci.lane_plan import (
    PlanFailure,
    Record,
    Table,
    assign,
    identity,
    module_of,
)
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.measurements.storage_evidence_fixture import write_junit
from tests.scaffold_evidence import (
    CHILD_NAMES,
    ENVIRONMENTS,
    SCHEDULED_LANES,
    STORAGE_ARGUMENTS,
    ChildTiming,
    collection_nodes,
    environment_key,
    environment_lanes,
    general_nodes,
    lane_arguments,
    matrix_rows,
    observed_timings,
    plan_outputs,
    planned_counts,
    record_lane,
    required_host_fixtures,
    timing_lines,
    verify_scaffold,
    verify_scheduled,
    write_assignment,
)

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
REQUESTED = "b" * 40
GENERAL_NODES = (
    "tests/test_general.py::TestCase::test_value[sqlite-a::b/c]",
    "tests/test_second.py::test_value",
    "tests/measurements/test_value.py::test_value",
    "tests/api/test_value.py::test_value[sqlite]",
    "tests/api_v2/test_other.py::test_value",
    "tests/engine/test_graph.py::test_value",
    "tests/test_third.py::test_value",
    "tests/test_engine.py::test_value",
    "tests/cli/test_surface.py::test_value",
)
STORAGE_NODES = ("tests/plugins/storage/test_value.py::test_value[sqlite]",)
LOCAL_CONTRACT_NODES = (
    "tests/plugins/runtime/local/test_lock.py::test_registry_lock_is_bounded",
    "tests/plugins/runtime/local/test_lock.py::"
    "test_local_runtime_releases_resources[lock_timeout]",
)
LANES = 2
COUNTS = {environment_key(system, python): LANES for system, python, _ in ENVIRONMENTS}
DURATIONS = {
    module_of(node): Record(100 * (index + 1), 1)
    for index, node in enumerate(GENERAL_NODES)
}
TABLE = Table("1", "c" * 40, dict.fromkeys(COUNTS, DURATIONS))


def node_of(named: str) -> str:
    name, _, parameter = named.partition("[")
    module, _, function = name.rpartition(".")
    suffix = f"[{parameter}" if parameter else ""
    return f"{module.replace('.', '/')}.py::{function}{suffix}"


def local_nodes(operating_system: str) -> tuple[str, ...]:
    required = sorted(required_host_fixtures(operating_system))
    return (*(node_of(named) for named in required), *LOCAL_CONTRACT_NODES)


def identities(nodes: tuple[str, ...] | frozenset[str]) -> frozenset[str]:
    return frozenset(identity(node) for node in nodes)


def collection_text(nodes: tuple[str, ...]) -> str:
    return "\n".join(nodes) + f"\n\n{len(nodes)} tests collected in 0.01s\n"


def complete_nodes(operating_system: str, *extra: str) -> tuple[str, ...]:
    return (*GENERAL_NODES, *STORAGE_NODES, *extra, *local_nodes(operating_system))


def lane_directory(root: Path, operating_system: str, python: str, lane: str) -> Path:
    return root / f"scaffold-tests-{operating_system}-{python}-{lane}"


def general_files(directory: Path, lane: str, collected: tuple[str, ...]) -> None:
    (directory / "complete.txt").write_text(collection_text(collected))
    (directory / "complete.stderr").write_text("")
    (directory / "complete.exit").write_text("0\n")
    plan = assign(general_nodes(collected), DURATIONS, LANES)
    children = plan[int(lane.removeprefix("general-")) - 1]
    for name, child in zip(CHILD_NAMES, children, strict=True):
        write_junit(directory / f"{name}.xml", identities(child.nodes))
        (directory / f"{name}.log").write_text("1 passed\n")
        (directory / f"{name}.exit").write_text("0\n")


def lane_files(directory: Path, lane: str, operating_system: str) -> None:
    directory.mkdir(parents=True)
    (directory / "tests.exit").write_text("0\n")
    (directory / "tests.stdout").write_text("1 passed\n")
    (directory / "tests.stderr").write_text("")
    if lane == "storage":
        write_junit(directory / "junit.xml", identities(STORAGE_NODES))
    elif lane == "local-runtime":
        write_junit(directory / "junit.xml", identities(local_nodes(operating_system)))
    else:
        general_files(directory, lane, complete_nodes(operating_system))


def complete_evidence(root: Path) -> None:
    for operating_system, python, databases in ENVIRONMENTS:
        for lane in environment_lanes(LANES):
            directory = lane_directory(root, operating_system, python, lane)
            lane_files(directory, lane, operating_system)
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
                lane_arguments(operating_system, lane),
            )


def rehash(directory: Path, changed: list[str]) -> None:
    receipt = directory / "receipt.json"
    data = parse_json_object(json.loads(receipt.read_text()))
    files = parse_json_object(data["files"])
    for name in changed:
        files[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    data["files"] = files
    receipt.write_text(json.dumps(data))


def recollect(root: Path, operating_system: str, python: str, *extra: str) -> None:
    for lane in environment_lanes(LANES)[2:]:
        directory = lane_directory(root, operating_system, python, lane)
        collected = complete_nodes(operating_system, *extra)
        (directory / "complete.txt").write_text(collection_text(collected))
        rehash(directory, ["complete.txt"])


def verify(root: Path, counts: dict[str, int] = COUNTS) -> object:
    return verify_scaffold(root, "123", "1", REVISION, REQUESTED, counts, TABLE)


def local_total() -> int:
    return sum(len(local_nodes(system)) for system, _, _ in ENVIRONMENTS)


def test_scaffold_receipts_prove_complete_planned_execution(tmp_path: Path) -> None:
    complete_evidence(tmp_path)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED, COUNTS, TABLE)

    assert (result.environments, result.lanes, result.tests) == (
        3,
        12,
        30 + local_total(),
    )


@pytest.mark.parametrize("environment", ["ubuntu-24.04-3.15.0", "macos-15-3.11.17"])
def test_general_lanes_may_collect_the_same_tests_in_another_order(
    tmp_path: Path, environment: str
) -> None:
    complete_evidence(tmp_path)
    system, _, python = environment.rpartition("-")
    second = lane_directory(tmp_path, system, python, "general-2")
    reordered = complete_nodes(system)[::-1]
    (second / "complete.txt").write_text(collection_text(reordered))
    rehash(second, ["complete.txt"])

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED, COUNTS, TABLE)

    assert result.tests == 30 + local_total()


def test_observed_timings_cover_every_general_child(tmp_path: Path) -> None:
    complete_evidence(tmp_path)

    lanes = list(observed_timings(tmp_path, COUNTS, TABLE))

    timings = [timing for lane in lanes for timing in lane]
    assert len(lanes) == 3 * LANES
    assert len(timings) == 3 * LANES * len(CHILD_NAMES)
    assert sum(timing.planned for timing in timings) == 3 * 4500
    assert sum(timing.recorded for timing in timings) == 3 * 10 * 9


def test_timings_print_readable_lanes_before_failing_on_a_missing_one(
    tmp_path: Path,
) -> None:
    complete_evidence(tmp_path)
    missing = lane_directory(tmp_path, "macos-15", "3.11.17", "general-2")
    for path in missing.iterdir():
        path.unlink()
    missing.rmdir()
    command = [sys.executable, "-m", "tests.scaffold_evidence", "timings"]

    result = subprocess.run(
        [*command, str(tmp_path), json.dumps(COUNTS)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )

    printed = [
        line.partition(":")[0].rpartition(" ")[0]
        for line in result.stdout.decode().splitlines()
        if ": planned " in line
    ]
    assert result.returncode != 0
    assert b"EvidenceFailure" in result.stderr
    assert (
        printed
        == [
            f"{key} {lane}"
            for key in (
                "ubuntu-24.04-3.11.17",
                "ubuntu-24.04-3.15.0",
                "macos-15-3.11.17",
            )
            for lane in ("general-1", "general-2")
            for _ in CHILD_NAMES
        ][: 5 * len(CHILD_NAMES)]
    )


def untimed_report(path: Path) -> None:
    text = path.read_text()
    path.write_text(text.replace(' time="0.010"', ""))


@pytest.mark.parametrize(
    ("corruption", "failure"),
    [
        ("truncated report", ExpatError),
        ("untimed report", PlanFailure),
        ("interrupted collection", EvidenceFailure),
    ],
)
def test_timings_refuse_evidence_that_verification_also_refuses(
    tmp_path: Path, corruption: str, failure: type[Exception]
) -> None:
    complete_evidence(tmp_path)
    directory = lane_directory(tmp_path, "macos-15", "3.11.17", "general-2")
    if corruption == "truncated report":
        (directory / "child-2.xml").write_text("<testsuite><testcase")
        changed = "child-2.xml"
    elif corruption == "untimed report":
        untimed_report(directory / "child-2.xml")
        changed = "child-2.xml"
    else:
        (directory / "complete.txt").write_text("interrupted collection\n")
        changed = "complete.txt"
    rehash(directory, [changed])

    with pytest.raises(failure):
        list(observed_timings(tmp_path, COUNTS, TABLE))

    with pytest.raises(failure):
        verify(tmp_path)


@pytest.mark.parametrize("operating_system", ["macos-26", "ubuntu-24.04"])
def test_scaffold_evidence_refuses_relabelled_macos_receipt(
    tmp_path: Path, operating_system: str
) -> None:
    complete_evidence(tmp_path)
    receipt = tmp_path / "scaffold-tests-macos-15-3.11.17-storage" / "receipt.json"
    data = parse_json_object(json.loads(receipt.read_text()))
    data["operating_system"] = operating_system
    receipt.write_text(json.dumps(data))

    with pytest.raises((ValidationError, EvidenceFailure)):
        verify(tmp_path)


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing", "artifact has unexpected file coverage"),
        ("failed", "execution did not succeed"),
        ("skipped", "test did not pass"),
        ("mismatch", "differs from its assignment"),
        ("overlap", "children overlap"),
        ("collection", "collected different tests"),
    ],
)
def test_general_child_evidence_refuses_rehashed_semantic_corruption(
    tmp_path: Path, corruption: str, match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-macos-15-3.11.17-general-1"
    changed: list[str] = []
    if corruption == "missing":
        (directory / "child-2.xml").unlink()
    elif corruption == "failed":
        (directory / "child-2.exit").write_text("1\n")
        changed.append("child-2.exit")
    elif corruption == "skipped":
        (directory / "child-2.xml").write_text(
            '<testsuite><testcase classname="tests.test_second" name="test_value">'
            "<skipped/></testcase></testsuite>"
        )
        changed.append("child-2.xml")
    elif corruption == "mismatch":
        write_junit(directory / "child-2.xml", frozenset({"tests.wrong.test_value"}))
        changed.append("child-2.xml")
    elif corruption == "overlap":
        (directory / "child-2.xml").write_bytes(
            (directory / "child-1.xml").read_bytes()
        )
        changed.append("child-2.xml")
    else:
        (directory / "complete.txt").write_text(collection_text(GENERAL_NODES[:1]))
        changed.append("complete.txt")
    rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify(tmp_path)


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing lane", "exactly the planned lanes"),
        ("extra lane", "exactly the planned lanes"),
        ("fewer planned", "exactly the planned lanes"),
        ("more planned", "exactly the planned lanes"),
        ("reassigned", "differs from its assignment"),
        ("relabelled", "mismatched provenance"),
        ("different collection", "collected different tests"),
    ],
)
def test_scaffold_evidence_requires_exactly_the_planned_general_lanes(
    tmp_path: Path, corruption: str, match: str
) -> None:
    complete_evidence(tmp_path)
    first = lane_directory(tmp_path, "ubuntu-24.04", "3.15.0", "general-1")
    second = lane_directory(tmp_path, "ubuntu-24.04", "3.15.0", "general-2")
    counts = dict(COUNTS)
    if corruption == "missing lane":
        for path in second.iterdir():
            path.unlink()
        second.rmdir()
    elif corruption == "extra lane":
        lane_directory(tmp_path, "ubuntu-24.04", "3.15.0", "general-3").mkdir()
    elif corruption in {"fewer planned", "more planned"}:
        counts["ubuntu-24.04-3.15.0"] = 1 if corruption == "fewer planned" else 3
    elif corruption == "reassigned":
        report, other = first / "child-1.xml", second / "child-1.xml"
        first_data, second_data = report.read_bytes(), other.read_bytes()
        report.write_bytes(second_data)
        other.write_bytes(first_data)
        rehash(first, ["child-1.xml"])
        rehash(second, ["child-1.xml"])
    elif corruption == "relabelled":
        data = parse_json_object(json.loads((second / "receipt.json").read_text()))
        data["lane"] = "general-1"
        (second / "receipt.json").write_text(json.dumps(data))
    else:
        fewer = complete_nodes("ubuntu-24.04")[1:]
        (second / "complete.txt").write_text(collection_text(fewer))
        rehash(second, ["complete.txt"])

    with pytest.raises(EvidenceFailure, match=match):
        verify(tmp_path, counts)


@pytest.mark.parametrize(
    ("corruption", "failure", "match"),
    [
        ("missing", FileNotFoundError, "receipt.json"),
        ("extra", EvidenceFailure, "exactly the planned lanes"),
        ("stale", EvidenceFailure, "mismatched provenance"),
        ("hash", EvidenceFailure, "hash differs from its receipt"),
        ("failed", EvidenceFailure, "execution did not succeed"),
        ("skipped", EvidenceFailure, "test did not pass"),
        ("execution", EvidenceFailure, "do not partition"),
        ("union", EvidenceFailure, "do not partition"),
        ("unexecuted general", EvidenceFailure, "differs from its assignment"),
        ("overlap", EvidenceFailure, "duplicate tests"),
    ],
)
def test_scaffold_evidence_refuses_incomplete_or_mismatched_execution(
    tmp_path: Path, corruption: str, failure: type[Exception], match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-macos-15-3.11.17-storage"
    receipt = directory / "receipt.json"
    changed: list[str] = []
    if corruption == "missing":
        receipt.unlink()
    elif corruption == "extra":
        (tmp_path / "unexpected").mkdir()
    elif corruption == "stale":
        data = parse_json_object(json.loads(receipt.read_text()))
        data["run_id"] = "122"
        receipt.write_text(json.dumps(data))
    elif corruption == "hash":
        (directory / "tests.stdout").write_text("changed")
    elif corruption == "failed":
        (directory / "tests.exit").write_text("1\n")
        changed.append("tests.exit")
    elif corruption == "union":
        recollect(tmp_path, "macos-15", "3.11.17", "tests/plugins/storage/test_x.py::t")
    elif corruption == "unexecuted general":
        recollect(
            tmp_path, "macos-15", "3.11.17", "tests/test_unexecuted.py::test_value"
        )
    else:
        target = directory / "junit.xml"
        if corruption == "overlap":
            write_junit(target, identities((*STORAGE_NODES, GENERAL_NODES[1])))
        elif corruption == "execution":
            write_junit(target, frozenset({"tests.wrong.test_value"}))
        else:
            target.write_text(
                '<testsuites><testsuite><testcase classname="a" name="b">'
                "<skipped/></testcase></testsuite></testsuites>"
            )
        changed.append("junit.xml")
    if changed:
        rehash(directory, changed)

    with pytest.raises(failure, match=match):
        verify(tmp_path)


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
    "lane", ["integration", "conformance", "general-0", "general-9"]
)
def test_record_refuses_an_unplanned_lane_before_reading_evidence(
    tmp_path: Path, lane: str
) -> None:
    missing = tmp_path / "absent"

    with pytest.raises(EvidenceFailure, match="unknown environment or lane"):
        record_lane(
            missing,
            "123",
            "1",
            REVISION,
            REQUESTED,
            "macos-15",
            "3.11.17",
            "sqlite",
            lane,
            (),
        )


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing host fixture", "lacks its host fixture tests"),
        ("misplaced host fixture", "do not partition"),
        ("outside path", "outside its path"),
        ("arguments", "different pytest arguments"),
    ],
)
def test_local_runtime_lane_requires_its_host_fixture_tests(
    tmp_path: Path, corruption: str, match: str
) -> None:
    complete_evidence(tmp_path)
    directory = tmp_path / "scaffold-tests-ubuntu-24.04-3.11.17-local-runtime"
    nodes = local_nodes("ubuntu-24.04")
    changed: list[str] = []
    if corruption == "missing host fixture":
        kept = tuple(node for node in nodes if "network_filesystem" not in node)
        write_junit(directory / "junit.xml", identities(kept))
        changed.append("junit.xml")
    elif corruption == "misplaced host fixture":
        extra = "tests/plugins/runtime/local/test_host.py::test_host"
        recollect(tmp_path, "ubuntu-24.04", "3.11.17", extra)
    elif corruption == "outside path":
        write_junit(
            directory / "junit.xml",
            identities((*nodes, "tests/test_other.py::test_value")),
        )
        changed.append("junit.xml")
    else:
        data = parse_json_object(json.loads((directory / "receipt.json").read_text()))
        data["arguments"] = ["tests/plugins/runtime/local"]
        (directory / "receipt.json").write_text(json.dumps(data))
    rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify(tmp_path)


def add_executed_node(
    root: Path, operating_system: str, python: str, node: str
) -> None:
    if general_nodes((node,)):
        for lane in environment_lanes(LANES)[2:]:
            directory = lane_directory(root, operating_system, python, lane)
            general_files(directory, lane, complete_nodes(operating_system, node))
            reports = [f"{name}.xml" for name in CHILD_NAMES]
            rehash(directory, ["complete.txt", *reports])
        return
    recollect(root, operating_system, python, node)
    storage = lane_directory(root, operating_system, python, "storage")
    write_junit(storage / "junit.xml", identities((*STORAGE_NODES, node)))
    rehash(storage, ["junit.xml"])


@pytest.mark.parametrize(
    ("operating_system", "python", "node"),
    [
        (
            "ubuntu-24.04",
            "3.15.0",
            "tests/conformance/test_lifetime_transport.py::test_value",
        ),
        (
            "macos-15",
            "3.11.17",
            "tests/conformance/test_lifetime_journal_recovery.py::Case::test_value",
        ),
        (
            "macos-15",
            "3.11.17",
            "tests/plugins/storage/test_pressure_controller.py::test_value[sqlite]",
        ),
    ],
)
def test_scaffold_evidence_refuses_scheduled_modules_in_pull_request_lanes(
    tmp_path: Path, operating_system: str, python: str, node: str
) -> None:
    complete_evidence(tmp_path)
    add_executed_node(tmp_path, operating_system, python, node)

    with pytest.raises(EvidenceFailure, match="contains scheduled modules"):
        verify(tmp_path)


@pytest.mark.parametrize(
    "node",
    [
        "tests/plugins/storage/test_pressure_controller.py::test_value[sqlite]",
        "tests/conformance/test_lifetime_transport_boundaries.py::test_value",
    ],
)
def test_linux_pull_request_lanes_keep_unscheduled_neighbours(
    tmp_path: Path, node: str
) -> None:
    complete_evidence(tmp_path)
    add_executed_node(tmp_path, "ubuntu-24.04", "3.11.17", node)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED, COUNTS, TABLE)

    assert result.tests == 31 + local_total()


def test_macos_storage_receipt_requires_the_scheduled_ignores(tmp_path: Path) -> None:
    complete_evidence(tmp_path)
    receipt = tmp_path / "scaffold-tests-macos-15-3.11.17-storage" / "receipt.json"
    data = parse_json_object(json.loads(receipt.read_text()))
    data["arguments"] = list(STORAGE_ARGUMENTS)
    receipt.write_text(json.dumps(data))

    with pytest.raises(EvidenceFailure, match="different pytest arguments"):
        verify(tmp_path)


def test_lane_assignment_writes_planned_nodes_in_collection_order(
    tmp_path: Path,
) -> None:
    collected = complete_nodes("macos-15")
    complete = tmp_path / "complete.txt"
    complete.write_text(collection_text(collected))
    table = Table("1", "c" * 40, {"macos-15-3.11.17": DURATIONS})

    children = write_assignment(
        complete, "macos-15", "3.11.17", "general-2", "2", tmp_path, table
    )

    written = {
        name: (tmp_path / f"{name}.args").read_text().splitlines()
        for name in CHILD_NAMES
    }
    assert children == assign(general_nodes(collected), DURATIONS, 2)[1]
    for name, child in zip(CHILD_NAMES, children, strict=True):
        assert written[name] == [node for node in collected if node in child.nodes]


@pytest.mark.parametrize(("lane", "lanes"), [("general-3", "2"), ("general-1", "9")])
def test_lane_assignment_refuses_a_lane_outside_its_plan(
    tmp_path: Path, lane: str, lanes: str
) -> None:
    complete = tmp_path / "complete.txt"
    complete.write_text(collection_text(complete_nodes("macos-15")))

    with pytest.raises(EvidenceFailure, match="planned"):
        write_assignment(complete, "macos-15", "3.11.17", lane, lanes, tmp_path, TABLE)

    assert not any(tmp_path.glob("*.args"))


def test_plan_outputs_split_lane_rows_by_platform(tmp_path: Path) -> None:
    linux, macos = tmp_path / "linux.txt", tmp_path / "macos.txt"
    linux.write_text(collection_text(complete_nodes("ubuntu-24.04")))
    macos.write_text(collection_text(complete_nodes("macos-15")))

    outputs = plan_outputs(linux, macos, TABLE)

    counts = planned_counts(outputs["lanes"])
    rows = matrix_rows(counts)
    assert counts == dict.fromkeys(COUNTS, 1)
    assert json.loads(outputs["linux"]) == {"include": rows["linux"]}
    assert json.loads(outputs["macos"]) == {"include": rows["macos"]}
    assert {row["os"] for row in rows["linux"]} == {"ubuntu-24.04"}
    assert {row["os"] for row in rows["macos"]} == {"macos-15"}
    assert [row["lane"] for row in rows["macos"]] == [
        "static-checks",
        "storage",
        "local-runtime",
        "general-1",
    ]
    assert [row["lane"] for row in rows["linux"]].count("static-checks") == 2
    assert rows["macos"][0]["pytest-arguments"] == ""
    assert rows["macos"][1]["pytest-arguments"] == " ".join(STORAGE_ARGUMENTS)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.15.0": 1}',
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.15.0": 1, "macos-15-3.11.17": 0}',
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.15.0": 1, "macos-15-3.11.17": 9}',
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.15.0": 1, '
        '"macos-15-3.11.17": true}',
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.15.0": 1, '
        '"macos-15-3.11.17": 1, "macos-26-3.11.17": 1}',
        '{"ubuntu-24.04-3.11.17": 1, "ubuntu-24.04-3.11.17": 1, '
        '"ubuntu-24.04-3.15.0": 1, "macos-15-3.11.17": 1}',
    ],
)
def test_planned_counts_refuse_invalid_lane_counts(text: str) -> None:
    with pytest.raises(EvidenceFailure):
        planned_counts(text)


@pytest.mark.parametrize(("recorded", "notice"), [(1250, False), (1251, True)])
def test_timing_lines_mark_children_beyond_their_plan(
    recorded: int, notice: bool
) -> None:
    timing = ChildTiming("macos-15-3.11.17", "general-1", "child-1", 1000, recorded)

    lines = timing_lines(timing)

    assert lines.startswith("macos-15-3.11.17 general-1 child-1: planned 1.0 s")
    assert ("::notice title=Scaffold lane plan::" in lines) is notice


def scheduled_report(
    path: Path, modules: tuple[str, ...], *extra: str
) -> tuple[str, ...]:
    nodes = (*(f"{module}::test_value" for module in modules), *extra)
    write_junit(path, identities(nodes))
    return nodes


@pytest.mark.parametrize(("operating_system", "lane"), list(SCHEDULED_LANES))
def test_scheduled_lane_evidence_accepts_every_passing_module(
    tmp_path: Path, operating_system: str, lane: str
) -> None:
    modules = SCHEDULED_LANES[(operating_system, lane)]
    report = tmp_path / "junit.xml"
    nodes = scheduled_report(report, modules)

    result = verify_scheduled(report, operating_system, lane)

    assert result == {"modules": len(modules), "tests": len(nodes)}


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing module", "lacks a module"),
        ("outside module", "outside its modules"),
        ("neighbour module", "outside its modules"),
        ("failed test", "did not pass"),
        ("unknown lane", "unknown environment or lane"),
    ],
)
def test_scheduled_lane_evidence_refuses_incomplete_or_foreign_execution(
    tmp_path: Path, corruption: str, match: str
) -> None:
    modules = SCHEDULED_LANES[("ubuntu-24.04", "lifetime")]
    report = tmp_path / "junit.xml"
    lane = "lifetime"
    if corruption == "missing module":
        scheduled_report(report, modules[1:])
    elif corruption == "outside module":
        scheduled_report(report, modules, "tests/test_general.py::test_value")
    elif corruption == "neighbour module":
        scheduled_report(
            report,
            modules,
            "tests/conformance/test_lifetime_transport_boundaries.py::test_value",
        )
    elif corruption == "failed test":
        report.write_text(
            '<testsuite><testcase classname="tests.conformance.'
            'test_lifetime_resume" name="test_value"><failure/></testcase>'
            "</testsuite>"
        )
    else:
        scheduled_report(report, modules)
        lane = "storage"

    with pytest.raises(EvidenceFailure, match=match):
        verify_scheduled(report, "ubuntu-24.04", lane)
