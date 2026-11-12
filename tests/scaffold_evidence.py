import hashlib
import json
import re
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TextIO, cast

from pydantic import Field, StrictStr

from kora.protocols.common import BoundaryModel
from scripts.ci.lane_plan import (
    CHILDREN,
    MAXIMUM_LANES,
    SPECIAL_LANES,
    TABLE,
    Child,
    Durations,
    Table,
    assign,
    identity,
    junit_milliseconds,
    lane_count,
    load_table,
    module_of,
    recorded_table,
    table_text,
)
from tests.measurements.storage_evidence_common import (
    EvidenceFailure,
    junit_nodes,
    read_json,
    unique_object,
)

ENVIRONMENTS: tuple[tuple[str, str, str], ...] = (
    ("ubuntu-24.04", "3.11.17", "sqlite,postgresql"),
    ("ubuntu-24.04", "3.15.0", "sqlite,postgresql"),
    ("macos-15", "3.11.17", "sqlite"),
)
CHILD_NAMES = tuple(f"child-{index}" for index in range(1, CHILDREN + 1))
STORAGE_ARGUMENTS = (
    "tests/plugins/storage",
    "tests/conformance/test_artifact_storage_conformance.py",
    "tests/conformance/test_installed_conformance.py",
)
LOCAL_RUNTIME_PATH = "tests/plugins/runtime/local"
LOCAL_RUNTIME_ARGUMENTS = ("-m", "not measurement", LOCAL_RUNTIME_PATH)
SPECIAL_PATHS = (*STORAGE_ARGUMENTS, LOCAL_RUNTIME_PATH)
SPECIAL_MATRIX_ARGUMENTS = {
    "storage": " ".join(STORAGE_ARGUMENTS),
    "local-runtime": LOCAL_RUNTIME_PATH,
}
SCHEDULED_LIFETIME_MODULES = (
    "tests/conformance/test_lifetime_cleanup_controls.py",
    "tests/conformance/test_lifetime_direct_inputs.py",
    "tests/conformance/test_lifetime_helper_phases.py",
    "tests/conformance/test_lifetime_io_helper.py",
    "tests/conformance/test_lifetime_journal_recovery.py",
    "tests/conformance/test_lifetime_measurement_cohorts.py",
    "tests/conformance/test_lifetime_measurement_observer.py",
    "tests/conformance/test_lifetime_parent_exit.py",
    "tests/conformance/test_lifetime_pending_helpers.py",
    "tests/conformance/test_lifetime_recovery_claim.py",
    "tests/conformance/test_lifetime_recovery_ownership.py",
    "tests/conformance/test_lifetime_resume.py",
    "tests/conformance/test_lifetime_rpc_concurrency.py",
    "tests/conformance/test_lifetime_services.py",
    "tests/conformance/test_lifetime_transport.py",
)
SCHEDULED_MACOS_STORAGE_MODULES = (
    "tests/plugins/storage/test_pressure_controller.py",
    "tests/plugins/storage/test_pressure_image_creation.py",
    "tests/plugins/storage/test_pressure_native_release.py",
)
SCHEDULED_LANES: dict[tuple[str, str], tuple[str, ...]] = {
    ("ubuntu-24.04", "lifetime"): SCHEDULED_LIFETIME_MODULES,
    ("macos-15", "lifetime"): SCHEDULED_LIFETIME_MODULES,
    ("macos-15", "storage"): SCHEDULED_MACOS_STORAGE_MODULES,
}
LOCAL_RUNTIME_MODULE = "tests.plugins.runtime.local"
LOCAL_RUNTIME_CONTRACTS = (
    "test_state_root.test_state_root_and_socket_dir_refuse_unsafe_ownership",
    "test_registry.test_registry_states_and_release",
    "test_lock.test_registry_lock_is_bounded",
    "test_lock.test_local_runtime_releases_resources",
    "test_platform.test_platform_mechanisms",
)
COUNT = re.compile(r"[1-9][0-9]*")
MatrixRow = dict[str, str | int]


def scheduled_modules(operating_system: str) -> tuple[str, ...]:
    return tuple(
        module
        for (system, _), modules in SCHEDULED_LANES.items()
        if system == operating_system
        for module in modules
    )


def environment_key(operating_system: str, python: str) -> str:
    return f"{operating_system}-{python}"


def artifact_name(operating_system: str, python: str, lane: str) -> str:
    return f"scaffold-tests-{operating_system}-{python}-{lane}"


def general_lanes(count: int) -> tuple[str, ...]:
    return tuple(f"general-{index}" for index in range(1, count + 1))


def environment_lanes(count: int) -> tuple[str, ...]:
    return ("storage", "local-runtime", *general_lanes(count))


def lane_arguments(operating_system: str, lane: str) -> tuple[str, ...]:
    if (operating_system, lane) == ("macos-15", "storage"):
        ignored = tuple(f"--ignore={path}" for path in SCHEDULED_MACOS_STORAGE_MODULES)
        return (*ignored, *STORAGE_ARGUMENTS)
    if lane == "storage":
        return STORAGE_ARGUMENTS
    if lane == "local-runtime":
        return LOCAL_RUNTIME_ARGUMENTS
    return ()


def module_identity(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def scheduled_owner(node: str, modules: tuple[str, ...]) -> str | None:
    return next(
        (
            module
            for module in modules
            if node.startswith(f"{module_identity(module)}.")
        ),
        None,
    )


def verify_scheduled(report: Path, operating_system: str, lane: str) -> dict[str, int]:
    modules = SCHEDULED_LANES.get((operating_system, lane))
    if modules is None:
        raise EvidenceFailure("scheduled execution has an unknown environment or lane")
    nodes = junit_nodes(report, 16777216)
    owners = [scheduled_owner(node, modules) for node in nodes]
    if None in owners:
        raise EvidenceFailure("scheduled lane ran tests outside its modules")
    if frozenset(owners) != frozenset(modules):
        raise EvidenceFailure("scheduled lane lacks a module")
    return {"modules": len(modules), "tests": len(nodes)}


def required_host_fixtures(operating_system: str) -> frozenset[str]:
    ownership = ["foreign_owner", "foreign_ancestor"]
    platform = ["other_uid_member", "setuid_carrier_is_not_owned"]
    if operating_system == "ubuntu-24.04":
        ownership.append("network_filesystem")
        platform.append("child_namespace_member")
    owned = (
        f"{LOCAL_RUNTIME_MODULE}.{LOCAL_RUNTIME_CONTRACTS[0]}[{leg}-{target}]"
        for leg in ownership
        for target in ("runtime", "socket_directory")
    )
    prepared = (
        f"{LOCAL_RUNTIME_MODULE}.{LOCAL_RUNTIME_CONTRACTS[4]}[{leg}]"
        for leg in platform
    )
    full = f"{LOCAL_RUNTIME_MODULE}.{LOCAL_RUNTIME_CONTRACTS[1]}"
    return frozenset([*owned, *prepared, f"{full}[failed_writes_leave_no_temporaries]"])


def validate_local_runtime(operating_system: str, selected: frozenset[str]) -> None:
    if not required_host_fixtures(operating_system) <= selected:
        raise EvidenceFailure("local runtime lane lacks its host fixture tests")
    for contract in LOCAL_RUNTIME_CONTRACTS:
        prefix = f"{LOCAL_RUNTIME_MODULE}.{contract}"
        if not any(
            node == prefix or node.startswith(f"{prefix}[") for node in selected
        ):
            raise EvidenceFailure("local runtime lane lacks a contract test")
    if any(not node.startswith(f"{LOCAL_RUNTIME_MODULE}.") for node in selected):
        raise EvidenceFailure("local runtime lane selected tests outside its path")


Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$")]
Lane = Annotated[
    StrictStr, Field(pattern=r"^(?:general-[1-9][0-9]*|storage|local-runtime)$")
]


class LaneReceipt(BoundaryModel):
    version: Literal[2]
    run_id: Identifier
    run_attempt: Identifier
    checkout_revision: Revision
    requested_revision: Revision
    operating_system: Literal["ubuntu-24.04", "macos-15"]
    python: Literal["3.11.17", "3.15.0"]
    databases: Literal["sqlite", "sqlite,postgresql"]
    lane: Lane
    arguments: tuple[StrictStr, ...]
    files: dict[StrictStr, Digest]


@dataclass(frozen=True)
class ChildTiming:
    environment: str
    lane: str
    child: str
    planned: int
    recorded: int


@dataclass(frozen=True)
class Verification:
    environments: int
    lanes: int
    tests: int


def evidence_bytes(path: Path, maximum: int = 16777216) -> bytes:
    information = path.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_size > maximum:
        raise EvidenceFailure("scaffold evidence is not a bounded regular file")
    with path.open("rb") as stream:
        encoded = stream.read(maximum + 1)
    if len(encoded) > maximum:
        raise EvidenceFailure("scaffold evidence grew beyond its input bound")
    return encoded


def collection_order(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise ValueError("missing ordinary collection")
    lines = [line for line in evidence_bytes(path).decode("utf-8").splitlines() if line]
    if not lines:
        raise ValueError("empty ordinary collection")
    summary = re.fullmatch(
        r"([1-9][0-9]*)(?:/([1-9][0-9]*))? tests? collected"
        r"(?: \(([1-9][0-9]*) deselected\))? in [0-9]+\.[0-9]+s"
        r"(?: \([0-9]+:[0-5][0-9]:[0-5][0-9]\))?",
        lines[-1],
    )
    if summary is None:
        raise ValueError("incomplete ordinary collection summary")
    selected = int(summary[1])
    total = int(summary[2]) if summary[2] is not None else selected
    deselected = int(summary[3]) if summary[3] is not None else 0
    nodes = tuple(lines[:-1])
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
    return nodes


def collection(path: Path) -> frozenset[str]:
    return frozenset(collection_order(path))


def identities(nodes: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for node in nodes:
        named = identity(node)
        if named in result:
            raise EvidenceFailure("scaffold collection contains duplicate tests")
        result.add(named)
    if not result:
        raise EvidenceFailure("scaffold collection is empty")
    return frozenset(result)


def validated_collection(path: Path) -> tuple[str, ...]:
    try:
        return collection_order(path)
    except ValueError:
        raise EvidenceFailure("scaffold collection is incomplete") from None


def collection_nodes(path: Path) -> frozenset[str]:
    return identities(validated_collection(path))


def is_general(node: str) -> bool:
    module = module_of(node)
    return not any(
        module == path or module.startswith(f"{path}/") for path in SPECIAL_PATHS
    )


def general_nodes(nodes: Iterable[str]) -> tuple[str, ...]:
    return tuple(node for node in nodes if is_general(node))


def environment_durations(table: Table, key: str) -> Durations:
    durations = table.environments.get(key)
    if durations is None:
        raise EvidenceFailure("the lane duration table lacks an environment")
    return durations


def expected_files(lane: str) -> frozenset[str]:
    files = {"tests.stdout", "tests.stderr", "tests.exit"}
    if lane in SPECIAL_LANES:
        files.add("junit.xml")
    else:
        files.update({"complete.txt", "complete.stderr", "complete.exit"})
        files.update(
            f"{name}.{suffix}"
            for name in CHILD_NAMES
            for suffix in ("log", "xml", "exit")
        )
    return frozenset(files)


def report_names(lane: str) -> tuple[str, ...]:
    return ("junit",) if lane in SPECIAL_LANES else CHILD_NAMES


def validate_lane(directory: Path, receipt: LaneReceipt) -> dict[str, frozenset[str]]:
    expected = expected_files(receipt.lane)
    if receipt.files.keys() != expected:
        raise EvidenceFailure("scaffold receipt has unexpected file coverage")
    if frozenset(path.name for path in directory.iterdir()) != expected | {
        "receipt.json"
    }:
        raise EvidenceFailure("scaffold artifact has unexpected file coverage")
    if receipt.arguments != lane_arguments(receipt.operating_system, receipt.lane):
        raise EvidenceFailure("scaffold receipt has different pytest arguments")
    for filename, digest in receipt.files.items():
        encoded = evidence_bytes(directory / filename)
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise EvidenceFailure("scaffold evidence hash differs from its receipt")
        if filename.endswith(".exit") and encoded != b"0\n":
            raise EvidenceFailure("scaffold execution did not succeed")
    reports: dict[str, frozenset[str]] = {}
    for name in report_names(receipt.lane):
        report = directory / f"{name}.xml"
        nodes = junit_nodes(report, 16777216)
        if not nodes or any(nodes & other for other in reports.values()):
            raise EvidenceFailure("lane children overlap or are empty")
        if receipt.lane not in SPECIAL_LANES:
            junit_milliseconds(report)
        reports[name] = nodes
    if receipt.lane == "local-runtime":
        validate_local_runtime(receipt.operating_system, reports["junit"])
    return reports


def record_lane(
    directory: Path,
    run_id: str,
    run_attempt: str,
    checkout_revision: str,
    requested_revision: str,
    operating_system: str,
    python: str,
    databases: str,
    lane: str,
    arguments: tuple[str, ...],
) -> LaneReceipt:
    if (operating_system, python, databases) not in ENVIRONMENTS or (
        lane not in environment_lanes(MAXIMUM_LANES)
    ):
        raise EvidenceFailure("scaffold execution has an unknown environment or lane")
    receipt = LaneReceipt.model_validate(
        {
            "version": 2,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "checkout_revision": checkout_revision,
            "requested_revision": requested_revision,
            "operating_system": operating_system,
            "python": python,
            "databases": databases,
            "lane": lane,
            "arguments": arguments,
            "files": {
                filename: hashlib.sha256(
                    evidence_bytes(directory / filename)
                ).hexdigest()
                for filename in expected_files(lane)
            },
        }
    )
    (directory / "receipt.json").write_text(receipt.model_dump_json(indent=2))
    validate_lane(directory, receipt)
    return receipt


def planned_count(text: str) -> int:
    if COUNT.fullmatch(text) is None or int(text) > MAXIMUM_LANES:
        raise EvidenceFailure("the planned general lane count is invalid")
    return int(text)


def planned_counts(text: str) -> dict[str, int]:
    document: object = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(document, dict):
        raise EvidenceFailure("the planned lane counts are not an object")
    expected = {environment_key(system, python) for system, python, _ in ENVIRONMENTS}
    counts: dict[str, int] = {}
    for key, value in cast(dict[str, object], document).items():
        if key not in expected or type(value) is not int:
            raise EvidenceFailure("the planned lane counts are invalid")
        if not 1 <= value <= MAXIMUM_LANES:
            raise EvidenceFailure("the planned general lane count is invalid")
        counts[key] = value
    if counts.keys() != expected:
        raise EvidenceFailure("the planned lane counts lack an environment")
    return counts


def environment_counts(
    collections: Mapping[str, tuple[str, ...]], table: Table
) -> dict[str, int]:
    return {
        key: lane_count(
            general_nodes(collections[key]), environment_durations(table, key)
        )
        for key in (
            environment_key(system, python) for system, python, _ in ENVIRONMENTS
        )
    }


def matrix_rows(counts: Mapping[str, int]) -> dict[str, list[MatrixRow]]:
    rows: dict[str, list[MatrixRow]] = {"linux": [], "macos": []}
    for system, python, databases in ENVIRONMENTS:
        count = counts[environment_key(system, python)]
        for lane in environment_lanes(count):
            rows["macos" if system == "macos-15" else "linux"].append(
                {
                    "os": system,
                    "python": python,
                    "databases": databases,
                    "lane": lane,
                    "lanes": count,
                    "pytest-arguments": SPECIAL_MATRIX_ARGUMENTS.get(lane, ""),
                }
            )
    return rows


def compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def plan_outputs(linux: Path, macos: Path, table: Table) -> dict[str, str]:
    collections = {
        environment_key(system, python): validated_collection(
            macos if system == "macos-15" else linux
        )
        for system, python, _ in ENVIRONMENTS
    }
    counts = environment_counts(collections, table)
    rows = matrix_rows(counts)
    for key, count in counts.items():
        nodes = general_nodes(collections[key])
        plan = assign(nodes, environment_durations(table, key), count)
        largest = max(child.milliseconds for lane in plan for child in lane)
        planned = sum(child.milliseconds for lane in plan for child in lane)
        sys.stderr.write(
            f"{key}: {len(nodes)} general tests, {planned / 1000:.1f} s planned in "
            f"{count} lanes, largest child {largest / 1000:.1f} s\n"
        )
    return {
        "linux": compact({"include": rows["linux"]}),
        "macos": compact({"include": rows["macos"]}),
        "lanes": compact(counts),
    }


def write_assignment(
    complete: Path,
    operating_system: str,
    python: str,
    lane: str,
    lanes: str,
    output: Path,
    table: Table,
) -> tuple[Child, ...]:
    count = planned_count(lanes)
    if lane not in general_lanes(count):
        raise EvidenceFailure("the general lane is outside its planned count")
    if (operating_system, python) not in {(s, p) for s, p, _ in ENVIRONMENTS}:
        raise EvidenceFailure("scaffold execution has an unknown environment or lane")
    nodes = general_nodes(validated_collection(complete))
    durations = environment_durations(table, environment_key(operating_system, python))
    children = assign(nodes, durations, count)[general_lanes(count).index(lane)]
    for name, child in zip(CHILD_NAMES, children, strict=True):
        (output / f"{name}.args").write_text(
            "".join(f"{node}\n" for node in nodes if node in child.nodes)
        )
    return children


def lane_receipt(directory: Path, provenance: tuple[str, ...]) -> LaneReceipt:
    if directory.is_symlink() or not directory.is_dir():
        raise EvidenceFailure("scaffold lane is not an owned directory")
    evidence_bytes(directory / "receipt.json", 16384)
    receipt = LaneReceipt.model_validate(read_json(directory / "receipt.json", 16384))
    actual = (
        receipt.run_id,
        receipt.run_attempt,
        receipt.checkout_revision,
        receipt.requested_revision,
        receipt.operating_system,
        receipt.python,
        receipt.databases,
        receipt.lane,
    )
    if actual != provenance:
        raise EvidenceFailure("scaffold receipt has mismatched provenance")
    return receipt


def require_assignment(
    children: tuple[Child, ...], reports: Mapping[str, frozenset[str]]
) -> None:
    for name, child in zip(CHILD_NAMES, children, strict=True):
        if reports[name] != frozenset(identity(node) for node in child.nodes):
            raise EvidenceFailure("a general lane child differs from its assignment")


def lane_timings(
    directory: Path, key: str, lane: str, count: int, table: Table
) -> list[ChildTiming]:
    nodes = general_nodes(validated_collection(directory / "complete.txt"))
    plan = assign(nodes, environment_durations(table, key), count)
    return [
        ChildTiming(
            key,
            lane,
            name,
            child.milliseconds,
            sum(junit_milliseconds(directory / f"{name}.xml").values()),
        )
        for name, child in zip(
            CHILD_NAMES, plan[general_lanes(count).index(lane)], strict=True
        )
    ]


def observed_timings(
    root: Path, counts: Mapping[str, int], table: Table
) -> Iterator[list[ChildTiming]]:
    for system, python, _ in ENVIRONMENTS:
        key = environment_key(system, python)
        for lane in general_lanes(counts[key]):
            directory = root / artifact_name(system, python, lane)
            yield lane_timings(directory, key, lane, counts[key], table)


def report_timings(
    root: Path, counts: Mapping[str, int], table: Table, output: TextIO
) -> None:
    for timings in observed_timings(root, counts, table):
        output.write("".join(map(timing_lines, timings)))
        output.flush()


def verify_scaffold(
    root: Path,
    run_id: str,
    run_attempt: str,
    checkout_revision: str,
    requested_revision: str,
    counts: Mapping[str, int],
    table: Table,
) -> Verification:
    expected = {
        artifact_name(system, python, lane)
        for system, python, _ in ENVIRONMENTS
        for lane in environment_lanes(counts[environment_key(system, python)])
    }
    if {path.name for path in root.iterdir()} != expected:
        raise EvidenceFailure(
            "scaffold evidence does not cover exactly the planned lanes"
        )
    total = 0
    for system, python, databases in ENVIRONMENTS:
        key = environment_key(system, python)
        lanes = environment_lanes(counts[key])
        reports: dict[str, dict[str, frozenset[str]]] = {}
        collections: list[frozenset[str]] = []
        for lane in lanes:
            directory = root / artifact_name(system, python, lane)
            provenance = (
                run_id,
                run_attempt,
                checkout_revision,
                requested_revision,
                system,
                python,
                databases,
                lane,
            )
            reports[lane] = validate_lane(
                directory, lane_receipt(directory, provenance)
            )
            if lane not in SPECIAL_LANES:
                collected = validated_collection(directory / "complete.txt")
                collections.append(frozenset(collected))
        if any(nodes != collections[0] for nodes in collections):
            raise EvidenceFailure("general lanes collected different tests")
        ordered = sorted(collections[0])
        complete = identities(ordered)
        scheduled = scheduled_modules(system)
        if any(scheduled_owner(node, scheduled) for node in complete):
            raise EvidenceFailure("scaffold collection contains scheduled modules")
        plan = assign(
            general_nodes(ordered), environment_durations(table, key), counts[key]
        )
        for lane, children in zip(general_lanes(counts[key]), plan, strict=True):
            require_assignment(children, reports[lane])
        inventories = [nodes for lane in reports.values() for nodes in lane.values()]
        combined = frozenset[str]().union(*inventories)
        if sum(len(nodes) for nodes in inventories) != len(combined):
            raise EvidenceFailure("scaffold lanes contain duplicate tests")
        if combined != complete:
            raise EvidenceFailure("scaffold lanes do not partition the complete suite")
        total += len(complete)
    return Verification(len(ENVIRONMENTS), len(expected), total)


def timing_lines(timing: ChildTiming) -> str:
    planned = f"{timing.planned / 1000:.1f} s"
    recorded = f"{timing.recorded / 1000:.1f} s"
    child = f"{timing.environment} {timing.lane} {timing.child}"
    line = f"{child}: planned {planned}, recorded {recorded}\n"
    if timing.recorded * 4 > timing.planned * 5:
        line += (
            f"::notice title=Scaffold lane plan::{child} "
            f"recorded {recorded} against a plan of {planned}; "
            "refresh scripts/ci/lane_durations.json\n"
        )
    return line


def refresh_durations(artifacts: Path) -> Table:
    table = recorded_table(artifacts, validated_collection)
    TABLE.write_text(table_text(table))
    return table


def main(arguments: list[str]) -> None:
    match arguments:
        case ["scheduled", report, operating_system, lane]:
            result = verify_scheduled(Path(report), operating_system, lane)
            sys.stdout.write(json.dumps(result) + "\n")
        case ["matrix", linux, macos]:
            outputs = plan_outputs(Path(linux), Path(macos), load_table())
            sys.stdout.write(
                "".join(f"{name}={value}\n" for name, value in outputs.items())
            )
        case ["assign", complete, operating_system, python, lane, lanes, output]:
            children = write_assignment(
                Path(complete),
                operating_system,
                python,
                lane,
                lanes,
                Path(output),
                load_table(),
            )
            for name, child in zip(CHILD_NAMES, children, strict=True):
                sys.stdout.write(
                    f"{lane} {name}: {len(child.nodes)} tests, "
                    f"{child.milliseconds / 1000:.1f} s planned\n"
                )
        case [
            "record",
            directory,
            run_id,
            attempt,
            checkout,
            requested,
            operating_system,
            python,
            databases,
            lane,
            *selection,
        ]:
            record_lane(
                Path(directory),
                run_id,
                attempt,
                checkout,
                requested,
                operating_system,
                python,
                databases,
                lane,
                tuple(selection),
            )
        case ["durations", artifacts]:
            table = refresh_durations(Path(artifacts))
            for key, durations in sorted(table.environments.items()):
                total = sum(record.milliseconds for record in durations.values())
                sys.stdout.write(
                    f"{key}: {len(durations)} modules, {total / 1000:.1f} s\n"
                )
        case ["timings", directory, lanes]:
            report_timings(
                Path(directory), planned_counts(lanes), load_table(), sys.stdout
            )
        case ["verify", directory, run_id, attempt, checkout, requested, lanes]:
            verification = verify_scaffold(
                Path(directory),
                run_id,
                attempt,
                checkout,
                requested,
                planned_counts(lanes),
                load_table(),
            )
            summary = {
                "environments": verification.environments,
                "lanes": verification.lanes,
                "tests": verification.tests,
            }
            sys.stdout.write(json.dumps(summary) + "\n")
        case _:
            raise EvidenceFailure("unknown scaffold evidence operation")


if __name__ == "__main__":
    main(sys.argv[1:])
