import heapq
import json
import math
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from xml.parsers.expat import ParserCreate

TARGET_MILLISECONDS = 270_000
CHILDREN = 3
MAXIMUM_LANES = 8
SPECIAL_LANES = frozenset({"storage", "local-runtime"})
TABLE = Path(__file__).with_name("lane_durations.json")
MAXIMUM_INPUT = 67108864
ARTIFACT = re.compile(
    r"scaffold-tests-(ubuntu-24\.04|macos-15)-([0-9]+\.[0-9]+\.[0-9]+)-(.+)"
)
REVISION = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[1-9][0-9]*")


class PlanFailure(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    milliseconds: int
    nodes: int


Durations = Mapping[str, Record]


@dataclass(frozen=True)
class Table:
    run: str
    revision: str
    environments: Mapping[str, Durations]


@dataclass(frozen=True)
class Child:
    nodes: frozenset[str]
    milliseconds: int


Plan = tuple[tuple[Child, ...], ...]


@dataclass(frozen=True)
class Item:
    estimate: int
    key: str
    nodes: tuple[str, ...]


def module_of(node: str) -> str:
    return node.partition("::")[0]


def identity(node: str) -> str:
    filename, _, test = node.partition("::")
    name, bracket, parameter = test.partition("[")
    return (
        filename.removesuffix(".py").replace("/", ".")
        + "."
        + name.replace("::", ".")
        + bracket
        + parameter
    )


def per_node(record: Record) -> int:
    return max(1, math.ceil(record.milliseconds / record.nodes))


def estimates(nodes: Iterable[str], durations: Durations) -> dict[str, int]:
    recorded = sum(record.milliseconds for record in durations.values())
    counted = sum(record.nodes for record in durations.values())
    if counted == 0:
        raise PlanFailure("the duration table has no recorded tests")
    default = Record(recorded, counted)
    return {node: per_node(durations.get(module_of(node), default)) for node in nodes}


def packing_items(estimated: Mapping[str, int], target: int) -> list[Item]:
    modules: dict[str, list[str]] = {}
    for node in sorted(estimated):
        modules.setdefault(module_of(node), []).append(node)
    items: list[Item] = []
    for module, members in modules.items():
        total = sum(estimated[node] for node in members)
        if total <= target:
            items.append(Item(total, module, tuple(members)))
        else:
            items.extend(Item(estimated[node], node, (node,)) for node in members)
    return sorted(items, key=lambda item: (-item.estimate, item.key))


def assign(
    nodes: Iterable[str],
    durations: Durations,
    lanes: int,
    children: int = CHILDREN,
    target: int = TARGET_MILLISECONDS,
) -> Plan:
    if not 1 <= lanes <= MAXIMUM_LANES or children < 1:
        raise PlanFailure("the lane plan has an invalid lane or child count")
    slots = lanes * children
    heap = [(0, slot) for slot in range(slots)]
    members: list[list[str]] = [[] for _ in range(slots)]
    loads = [0] * slots
    for item in packing_items(estimates(nodes, durations), target):
        load, slot = heapq.heappop(heap)
        members[slot].extend(item.nodes)
        loads[slot] = load + item.estimate
        heapq.heappush(heap, (loads[slot], slot))
    if not all(members):
        raise PlanFailure("a planned lane child has no tests")
    return tuple(
        tuple(
            Child(frozenset(members[slot]), loads[slot])
            for slot in range(lane * children, (lane + 1) * children)
        )
        for lane in range(lanes)
    )


def lane_count(
    nodes: Iterable[str],
    durations: Durations,
    children: int = CHILDREN,
    target: int = TARGET_MILLISECONDS,
) -> int:
    selected = frozenset(nodes)
    for lanes in range(1, MAXIMUM_LANES + 1):
        plan = assign(selected, durations, lanes, children, target)
        if all(within_target(child, target) for lane in plan for child in lane):
            return lanes
    raise PlanFailure(f"the collection needs more than {MAXIMUM_LANES} lanes")


def within_target(child: Child, target: int) -> bool:
    return child.milliseconds <= target or len(child.nodes) == 1


def bounded_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PlanFailure("lane timing input is not a regular file")
    with path.open("rb") as stream:
        encoded = stream.read(MAXIMUM_INPUT + 1)
    if len(encoded) > MAXIMUM_INPUT:
        raise PlanFailure("lane timing input exceeds its bound")
    return encoded


def refuse_document_type(
    name: str, system_id: str | None, public_id: str | None, subset: int
) -> None:
    raise PlanFailure("lane timing input cannot declare a document type")


def recorded_milliseconds(value: str | None) -> int:
    seconds = math.nan if value is None else float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise PlanFailure("a JUnit test case has no finite duration")
    return round(seconds * 1000)


def junit_milliseconds(path: Path) -> dict[str, int]:
    durations: dict[str, int] = {}

    def start(name: str, attributes: dict[str, str]) -> None:
        if name != "testcase":
            return
        classname, test = attributes.get("classname"), attributes.get("name")
        case = f"{classname}.{test}"
        if not classname or not test or case in durations:
            raise PlanFailure("a JUnit test case is unnamed or duplicated")
        durations[case] = recorded_milliseconds(attributes.get("time"))

    parser = ParserCreate()
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse_document_type
    parser.Parse(bounded_bytes(path), True)
    return durations


def collected_modules(path: Path) -> dict[str, str]:
    lines = bounded_bytes(path).decode("utf-8").splitlines()
    return {identity(line): module_of(line) for line in lines if "::" in line}


def receipt_source(path: Path) -> tuple[str, str]:
    document: object = json.loads(bounded_bytes(path))
    if not isinstance(document, dict):
        raise PlanFailure("a lane receipt is not an object")
    fields = cast(dict[str, object], document)
    run, revision = fields.get("run_id"), fields.get("checkout_revision")
    if not isinstance(run, str) or IDENTIFIER.fullmatch(run) is None:
        raise PlanFailure("a lane receipt has no run")
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise PlanFailure("a lane receipt has no revision")
    return run, revision


def general_lane_directories(root: Path) -> dict[str, list[Path]]:
    environments: dict[str, list[Path]] = {}
    for directory in sorted(root.iterdir()):
        match = ARTIFACT.fullmatch(directory.name)
        if match is None or directory.is_symlink() or not directory.is_dir():
            raise PlanFailure("the timing input has an unexpected entry")
        if match[3] not in SPECIAL_LANES:
            environments.setdefault(f"{match[1]}-{match[2]}", []).append(directory)
    return environments


def environment_durations(directories: list[Path]) -> dict[str, Record]:
    collections = [
        directory / "complete.txt"
        for directory in directories
        if (directory / "complete.txt").is_file()
    ]
    if not collections:
        raise PlanFailure("an environment has no recorded collection")
    owners = collected_modules(collections[0])
    totals: dict[str, tuple[int, int]] = {}
    for directory in directories:
        for report in sorted(directory.glob("*.xml")):
            for case, milliseconds in junit_milliseconds(report).items():
                if case not in owners:
                    raise PlanFailure("a recorded test is not in the collection")
                recorded, counted = totals.get(owners[case], (0, 0))
                totals[owners[case]] = (recorded + milliseconds, counted + 1)
    return {module: Record(*total) for module, total in sorted(totals.items())}


def recorded_table(root: Path) -> Table:
    environments = general_lane_directories(root)
    sources = {
        receipt_source(directory / "receipt.json")
        for directory in sorted(root.iterdir())
    }
    if len(sources) != 1 or not environments:
        raise PlanFailure("the timing input does not come from one complete run")
    run, revision = sources.pop()
    return Table(
        run,
        revision,
        {
            environment: environment_durations(directories)
            for environment, directories in sorted(environments.items())
        },
    )


def table_text(table: Table) -> str:
    sections = [
        f"    {json.dumps(environment)}: {{\n"
        + ",\n".join(
            f"      {json.dumps(module)}: [{record.milliseconds}, {record.nodes}]"
            for module, record in sorted(durations.items())
        )
        + "\n    }"
        for environment, durations in sorted(table.environments.items())
    ]
    return (
        "{\n"
        '  "version": 1,\n'
        f'  "run": {json.dumps(table.run)},\n'
        f'  "revision": {json.dumps(table.revision)},\n'
        '  "environments": {\n' + ",\n".join(sections) + "\n  }\n}\n"
    )


def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name, value in pairs:
        if name in fields:
            raise PlanFailure("the duration table repeats a field")
        fields[name] = value
    return fields


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlanFailure("the duration table has an invalid shape")
    return cast(dict[str, object], value)


def count(value: object, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise PlanFailure("a duration record has an invalid count")
    return value


def parsed_record(value: object) -> Record:
    pair = cast(list[object], value) if isinstance(value, list) else []
    if len(pair) != 2:
        raise PlanFailure("a duration record is not a pair")
    return Record(count(pair[0], 0), count(pair[1], 1))


def parsed_table(text: str) -> Table:
    document = object_fields(json.loads(text, object_pairs_hook=unique_fields))
    if set(document) != {"version", "run", "revision", "environments"}:
        raise PlanFailure("the duration table has unexpected fields")
    run, revision = document["run"], document["revision"]
    if document["version"] != 1 or not isinstance(run, str):
        raise PlanFailure("the duration table has an invalid version or run")
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise PlanFailure("the duration table has an invalid revision")
    if IDENTIFIER.fullmatch(run) is None:
        raise PlanFailure("the duration table has an invalid run")
    environments = {
        environment: {
            module: parsed_record(record)
            for module, record in object_fields(durations).items()
        }
        for environment, durations in object_fields(document["environments"]).items()
    }
    return Table(run, revision, environments)


def load_table(path: Path = TABLE) -> Table:
    return parsed_table(bounded_bytes(path).decode("utf-8"))


def main(arguments: list[str]) -> None:
    if len(arguments) not in (2, 3) or arguments[0] != "durations":
        raise PlanFailure("usage: durations <artifact-directory> [<table>]")
    table = recorded_table(Path(arguments[1]))
    output = Path(arguments[2]) if len(arguments) == 3 else TABLE
    output.write_text(table_text(table))
    for environment, durations in sorted(table.environments.items()):
        seconds = sum(record.milliseconds for record in durations.values()) / 1000
        sys.stdout.write(f"{environment}: {len(durations)} modules, {seconds:.1f} s\n")


if __name__ == "__main__":
    main(sys.argv[1:])
