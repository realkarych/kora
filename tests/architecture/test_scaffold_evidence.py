import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kora.ir.json import parse_json_object
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.measurements.storage_evidence_fixture import write_junit
from tests.scaffold_evidence import (
    COHORTS,
    ENVIRONMENTS,
    LANE_ARGUMENTS,
    LANE_CHILDREN,
    SCHEDULED_LANES,
    STORAGE_ARGUMENTS,
    collection_nodes,
    lane_arguments,
    record_lane,
    required_host_fixtures,
    verify_scaffold,
    verify_scheduled,
)

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


def node_of(identity: str) -> str:
    name, _, parameter = identity.partition("[")
    module, _, function = name.rpartition(".")
    suffix = f"[{parameter}" if parameter else ""
    return f"{module.replace('.', '/')}.py::{function}{suffix}"


def local_nodes(operating_system: str) -> tuple[str, ...]:
    required = sorted(required_host_fixtures(operating_system))
    return (*(node_of(identity) for identity in required), *LOCAL_CONTRACT_NODES)


def lane_nodes(lane: str, operating_system: str) -> tuple[str, ...]:
    if lane == "storage":
        return STORAGE_NODES
    if lane == "local-runtime":
        return local_nodes(operating_system)
    return GENERAL_NODES


def collection_text(nodes: tuple[str, ...]) -> str:
    return "\n".join(nodes) + f"\n\n{len(nodes)} tests collected in 0.01s\n"


def identities(nodes: tuple[str, ...]) -> frozenset[str]:
    result: set[str] = set()
    for node in nodes:
        filename, _, test = node.partition("::")
        name, bracket, parameter = test.partition("[")
        result.add(
            filename[:-3].replace("/", ".")
            + "."
            + name.replace("::", ".")
            + bracket
            + parameter
        )
    return frozenset(result)


def lane_files(root: Path, lane: str, operating_system: str) -> None:
    root.mkdir(parents=True)
    selected = lane_nodes(lane, operating_system)
    (root / "tests.exit").write_text("0\n")
    (root / "tests.stdout").write_text("1 passed\n")
    (root / "tests.stderr").write_text("")
    if lane == "conformance":
        (root / "complete.txt").write_text(
            collection_text(
                (*GENERAL_NODES, *STORAGE_NODES, *local_nodes(operating_system))
            )
        )
        (root / "complete.stderr").write_text("")
        (root / "complete.exit").write_text("0\n")
    children = LANE_CHILDREN[operating_system][lane]
    if children:
        groups: dict[str, str] = dict(zip(COHORTS, selected, strict=True))
        assigned: dict[str, tuple[str, ...]] = {
            name: (groups[name],) for name in children
        }
        if operating_system == "macos-15" and lane == "remainder":
            assigned["measurements"] = (groups["measurements"], groups["engine"])
            assigned["other"] = (groups["other"], groups["graph"])
        for name, nodes in assigned.items():
            write_junit(root / f"{name}.xml", identities(nodes))
            (root / f"{name}.log").write_text("1 passed\n")
            (root / f"{name}.exit").write_text("0\n")
    else:
        write_junit(root / "junit.xml", identities(selected))


def complete_evidence(root: Path) -> None:
    for operating_system, python, databases in ENVIRONMENTS:
        for lane in LANE_CHILDREN[operating_system]:
            directory = root / f"scaffold-tests-{operating_system}-{python}-{lane}"
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


def test_scaffold_receipts_prove_complete_disjoint_execution(tmp_path: Path) -> None:
    complete_evidence(tmp_path)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)

    local = sum(len(local_nodes(system)) for system, _, _ in ENVIRONMENTS)
    assert result == {"environments": 3, "lanes": 14, "tests": 30 + local}


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
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("missing", "artifact has unexpected file coverage"),
        ("failed", "execution did not succeed"),
        ("skipped", "test did not pass"),
        ("mismatch", "do not partition"),
        ("overlap", "ordinary cohorts overlap"),
        ("full", "do not partition"),
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
        (directory / "second.xml").write_bytes((directory / "first.xml").read_bytes())
        changed.append("second.xml")
    else:
        (directory / "complete.txt").write_text(collection_text((GENERAL_NODES[0],)))
        changed.append("complete.txt")
    rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


@pytest.mark.parametrize(
    ("corruption", "failure", "match"),
    [
        ("missing", FileNotFoundError, "receipt.json"),
        ("extra", EvidenceFailure, "exactly fourteen lanes"),
        ("stale", EvidenceFailure, "mismatched provenance"),
        ("hash", EvidenceFailure, "hash differs from its receipt"),
        ("failed", EvidenceFailure, "execution did not succeed"),
        ("skipped", EvidenceFailure, "test did not pass"),
        ("execution", EvidenceFailure, "do not partition"),
        ("union", EvidenceFailure, "do not partition"),
        ("overlap", EvidenceFailure, "duplicate tests"),
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
        (directory / "complete.txt").write_text(
            collection_text(
                (
                    *GENERAL_NODES,
                    *STORAGE_NODES,
                    *local_nodes("macos-15"),
                    "tests/test_unexecuted.py::test_value",
                )
            )
        )
        changed.append("complete.txt")
    else:
        target = directory / "junit.xml"
        if corruption == "overlap":
            write_junit(target, identities((*STORAGE_NODES, GENERAL_NODES[0])))
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
        ("missing lane", "exactly fourteen lanes"),
        ("unequal inventories", "unexpected file coverage"),
        ("different cohorts", "cohorts overlap"),
        ("wrong shard", "unexpected file coverage"),
        ("remainder junit", "do not partition"),
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
    elif corruption == "different cohorts":
        (directory / "other.xml").write_bytes(
            (directory / "measurements.xml").read_bytes()
        )
        changed.append("other.xml")
    elif corruption == "wrong shard":
        (directory / "integration.xml").write_text("1 passed\n")
    else:
        write_junit(directory / "other.xml", frozenset({"tests.wrong.test_value"}))
        changed.append("other.xml")
    if changed:
        rehash(directory, changed)

    with pytest.raises(EvidenceFailure, match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


def test_record_refuses_macos_integration_before_reading_evidence(
    tmp_path: Path,
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
            "integration",
            LANE_ARGUMENTS["integration"],
        )


@pytest.mark.parametrize(
    ("corruption", "failure", "match"),
    [
        ("missing", FileNotFoundError, "receipt.json"),
        ("macos", EvidenceFailure, "exactly fourteen lanes"),
        ("stale attempt", EvidenceFailure, "mismatched provenance"),
        ("relabelled", EvidenceFailure, "mismatched provenance"),
        (
            "swapped",
            EvidenceFailure,
            "integration lane differs from its inventory subset",
        ),
    ],
)
def test_linux_integration_evidence_requires_its_own_receipt_and_inventory(
    tmp_path: Path, corruption: str, failure: type[Exception], match: str
) -> None:
    complete_evidence(tmp_path)
    integration = tmp_path / "scaffold-tests-ubuntu-24.04-3.15.0-integration"
    remainder = tmp_path / "scaffold-tests-ubuntu-24.04-3.15.0-remainder"
    receipt = integration / "receipt.json"
    if corruption == "missing":
        receipt.unlink()
    elif corruption == "macos":
        (tmp_path / "scaffold-tests-macos-15-3.11.17-integration").mkdir()
    elif corruption == "swapped":
        report = integration / "integration.xml"
        other = remainder / "other.xml"
        integration_data, other_data = report.read_bytes(), other.read_bytes()
        report.write_bytes(other_data)
        other.write_bytes(integration_data)
        rehash(integration, ["integration.xml"])
        rehash(remainder, ["other.xml"])
    else:
        data = parse_json_object(json.loads(receipt.read_text()))
        if corruption == "stale attempt":
            data["run_attempt"] = "2"
        else:
            data["lane"] = "remainder"
        receipt.write_text(json.dumps(data))

    with pytest.raises(failure, match=match):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


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
        conformance = tmp_path / "scaffold-tests-ubuntu-24.04-3.11.17-conformance"
        (conformance / "complete.txt").write_text(
            collection_text(
                (
                    *GENERAL_NODES,
                    *STORAGE_NODES,
                    *nodes,
                    "tests/db/test_host.py::test_host",
                )
            )
        )
        rehash(conformance, ["complete.txt"])
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
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


def add_storage_node(root: Path, operating_system: str, python: str, node: str) -> None:
    conformance = root / f"scaffold-tests-{operating_system}-{python}-conformance"
    storage = root / f"scaffold-tests-{operating_system}-{python}-storage"
    (conformance / "complete.txt").write_text(
        collection_text(
            (*GENERAL_NODES, *STORAGE_NODES, node, *local_nodes(operating_system))
        )
    )
    write_junit(storage / "junit.xml", identities((*STORAGE_NODES, node)))
    rehash(conformance, ["complete.txt"])
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
    add_storage_node(tmp_path, operating_system, python, node)

    with pytest.raises(EvidenceFailure, match="contains scheduled modules"):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


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
    add_storage_node(tmp_path, "ubuntu-24.04", "3.11.17", node)

    result = verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)

    local = sum(len(local_nodes(system)) for system, _, _ in ENVIRONMENTS)
    assert result == {"environments": 3, "lanes": 14, "tests": 31 + local}


def test_macos_storage_receipt_requires_the_scheduled_ignores(tmp_path: Path) -> None:
    complete_evidence(tmp_path)
    receipt = tmp_path / "scaffold-tests-macos-15-3.11.17-storage" / "receipt.json"
    data = parse_json_object(json.loads(receipt.read_text()))
    data["arguments"] = list(STORAGE_ARGUMENTS)
    receipt.write_text(json.dumps(data))

    with pytest.raises(EvidenceFailure, match="different pytest arguments"):
        verify_scaffold(tmp_path, "123", "1", REVISION, REQUESTED)


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
