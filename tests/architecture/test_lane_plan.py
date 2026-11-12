import json
from pathlib import Path

import pytest

from scripts.ci.lane_plan import (
    MAXIMUM_LANES,
    TABLE,
    TARGET_MILLISECONDS,
    Plan,
    PlanFailure,
    Record,
    Table,
    assign,
    estimates,
    identity,
    lane_count,
    load_table,
    module_of,
    packing_items,
    parsed_table,
    recorded_table,
    table_text,
)
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.scaffold_evidence import validated_collection

REVISION = "c" * 40


def module_nodes(module: str, count: int) -> list[str]:
    return [f"{module}::test_value[{index}]" for index in range(count)]


def placed(plan: Plan) -> list[str]:
    return [node for lane in plan for child in lane for node in child.nodes]


def owners(plan: Plan, module: str) -> set[tuple[int, int]]:
    return {
        (lane_index, child_index)
        for lane_index, lane in enumerate(plan)
        for child_index, child in enumerate(lane)
        if any(module_of(node) == module for node in child.nodes)
    }


def uniform_suite(modules: int) -> tuple[list[str], dict[str, Record]]:
    names = [f"tests/test_module_{index:02d}.py" for index in range(modules)]
    nodes = [node for name in names for node in module_nodes(name, 2)]
    return nodes, {name: Record(100, 2) for name in names}


def test_assignment_places_every_collected_test_exactly_once() -> None:
    nodes = [
        *module_nodes("tests/test_large.py", 40),
        *module_nodes("tests/test_medium.py", 9),
        *module_nodes("tests/test_new.py", 5),
        *module_nodes("tests/api/test_small.py", 3),
    ]
    durations = {
        "tests/test_large.py": Record(4000, 40),
        "tests/test_medium.py": Record(900, 9),
        "tests/api/test_small.py": Record(30, 3),
    }

    plan = assign(nodes, durations, 2, 3, 1000)

    assert sorted(placed(plan)) == sorted(nodes)
    assert all(child.nodes for lane in plan for child in lane)
    assert [len(lane) for lane in plan] == [3, 3]


def test_lane_count_is_the_fewest_lanes_that_keep_every_child_within_target() -> None:
    nodes, durations = uniform_suite(12)

    lanes = lane_count(nodes, durations, 3, 200)

    within = assign(nodes, durations, lanes, 3, 200)
    fewer = assign(nodes, durations, lanes - 1, 3, 200)
    assert lanes == 2
    assert max(child.milliseconds for lane in within for child in lane) <= 200
    assert max(child.milliseconds for lane in fewer for child in lane) > 200


def test_module_above_the_target_is_split_across_children() -> None:
    nodes = [
        *module_nodes("tests/test_large.py", 6),
        *module_nodes("tests/test_small.py", 3),
    ]
    durations = {
        "tests/test_large.py": Record(600, 6),
        "tests/test_small.py": Record(30, 3),
    }

    plan = assign(nodes, durations, 1, 3, 250)

    assert owners(plan, "tests/test_large.py") == {(0, 0), (0, 1), (0, 2)}
    assert len(owners(plan, "tests/test_small.py")) == 1
    assert [child.milliseconds for child in plan[0]] == [230, 200, 200]


def test_oversized_module_is_split_into_contiguous_chunks_of_sorted_tests() -> None:
    large = module_nodes("tests/test_large.py", 10)
    small = module_nodes("tests/test_small.py", 3)
    durations = {
        "tests/test_large.py": Record(1000, 10),
        "tests/test_small.py": Record(30, 3),
    }

    items = packing_items(estimates([*small, *reversed(large)], durations), 300)

    assert [item.nodes for item in items] == [
        tuple(large[2:5]),
        tuple(large[7:10]),
        tuple(large[0:2]),
        tuple(large[5:7]),
        tuple(small),
    ]
    assert [item.estimate for item in items] == [300, 300, 200, 200, 30]


def contiguous(nodes: list[str], part: tuple[str, ...]) -> bool:
    start = nodes.index(part[0])
    return part == tuple(nodes[start : start + len(part)])


@pytest.mark.parametrize(
    ("tests", "milliseconds", "sizes"),
    [(87, 6151, [29, 29, 29]), (7, 100_000, [1, 2, 2, 2]), (5, 100_000, [1, 2, 2])],
)
def test_every_chunk_of_an_oversized_module_stays_within_the_target(
    tests: int, milliseconds: int, sizes: list[int]
) -> None:
    nodes = [f"tests/test_large.py::test_value[{index:03d}]" for index in range(tests)]
    fillers = [f"tests/test_filler_{index:02d}.py::test_value" for index in range(10)]
    durations = {
        "tests/test_large.py": Record(tests * milliseconds, tests),
        **{module_of(node): Record(10_000, 1) for node in fillers},
    }

    items = packing_items(estimates(nodes, durations), TARGET_MILLISECONDS)
    lanes = lane_count([*nodes, *fillers], durations)

    plan = assign([*nodes, *fillers], durations, lanes)
    assert sorted(len(item.nodes) for item in items) == sizes
    assert all(contiguous(nodes, item.nodes) for item in items)
    assert all(
        item.estimate <= TARGET_MILLISECONDS or len(item.nodes) == 1 for item in items
    )
    assert all(
        child.milliseconds <= TARGET_MILLISECONDS or len(child.nodes) == 1
        for lane in plan
        for child in lane
    )


def test_unknown_modules_use_the_mean_recorded_test_and_known_modules_scale() -> None:
    durations = {
        "tests/test_fast.py": Record(10, 10),
        "tests/test_slow.py": Record(990, 1),
    }
    nodes = [
        *module_nodes("tests/test_fast.py", 20),
        "tests/test_slow.py::test_value",
        *module_nodes("tests/test_new.py", 2),
    ]

    estimated = estimates(nodes, durations)

    assert sum(estimated[node] for node in module_nodes("tests/test_fast.py", 20)) == 20
    assert estimated["tests/test_slow.py::test_value"] == 990
    assert [estimated[node] for node in module_nodes("tests/test_new.py", 2)] == [
        91,
        91,
    ]


@pytest.mark.parametrize("order", ["reversed", "rotated", "interleaved"])
def test_assignment_is_independent_of_collection_order(order: str) -> None:
    nodes, durations = uniform_suite(17)
    nodes.extend(module_nodes("tests/test_unrecorded.py", 4))
    permuted = {
        "reversed": nodes[::-1],
        "rotated": nodes[7:] + nodes[:7],
        "interleaved": nodes[::2] + nodes[1::2],
    }[order]

    plan = assign(permuted, durations, 2, 3, 500)

    assert plan == assign(nodes, durations, 2, 3, 500)


def test_a_single_test_above_the_target_gets_a_child_of_its_own() -> None:
    short = [f"tests/test_short_{index:02d}.py" for index in range(30)]
    nodes = [
        "tests/test_long.py::test_value",
        *(f"{module}::test_value" for module in short),
    ]
    durations = {
        "tests/test_long.py": Record(500, 1),
        **{module: Record(10, 1) for module in short},
    }

    lanes = lane_count(nodes, durations, 3, 100)

    plan = assign(nodes, durations, lanes, 3, 100)
    assert lanes == 2
    assert plan[0][0].nodes == {"tests/test_long.py::test_value"}
    assert sorted(child.milliseconds for lane in plan for child in lane) == [
        *([60] * 5),
        500,
    ]


def test_lane_count_refuses_more_than_the_lane_bound() -> None:
    nodes, durations = uniform_suite(3 * (MAXIMUM_LANES + 1))

    with pytest.raises(PlanFailure, match="more than 8 lanes"):
        lane_count(nodes, durations, 3, 100)


@pytest.mark.parametrize(
    ("lanes", "children"), [(0, 3), (MAXIMUM_LANES + 1, 3), (1, 0)]
)
def test_assignment_refuses_an_invalid_shape(lanes: int, children: int) -> None:
    nodes, durations = uniform_suite(30)

    with pytest.raises(PlanFailure, match="invalid lane or child count"):
        assign(nodes, durations, lanes, children)


def test_assignment_refuses_a_child_without_tests() -> None:
    nodes, durations = uniform_suite(1)

    with pytest.raises(PlanFailure, match="no tests"):
        assign(nodes, durations, 1, 3, 1000)


def test_committed_table_covers_every_pull_request_environment() -> None:
    expected = {"macos-15-3.11.17", "ubuntu-24.04-3.11.17", "ubuntu-24.04-3.15.0"}

    table = load_table()

    assert set(table.environments) == expected
    assert all(table.environments.values())
    assert TABLE.read_text() == table_text(table)


def test_table_text_round_trips() -> None:
    table = Table(
        "123",
        REVISION,
        {
            "macos-15-3.11.17": {"tests/test_a.py": Record(0, 1)},
            "ubuntu-24.04-3.11.17": {
                "tests/test_a.py": Record(15, 2),
                "tests/test_b.py": Record(7, 1),
            },
        },
    )

    text = table_text(table)

    assert parsed_table(text) == table


@pytest.mark.parametrize(
    "document",
    [
        '{"version": 1, "version": 1, "run": "1", "revision": "%s", '
        '"environments": {}}',
        '{"version": 2, "run": "1", "revision": "%s", "environments": {}}',
        '{"version": 1, "run": "01", "revision": "%s", "environments": {}}',
        '{"version": 1, "run": "1", "revision": "abc", "environments": {}}',
        '{"version": 1, "run": "1", "revision": "%s", "environments": {}, "x": 1}',
        '{"version": 1, "run": "1", "revision": "%s", "environments": []}',
        '{"version": 1, "run": "1", "revision": "%s", '
        '"environments": {"e": {"tests/test_a.py": [-1, 1]}}}',
        '{"version": 1, "run": "1", "revision": "%s", '
        '"environments": {"e": {"tests/test_a.py": [1, 0]}}}',
        '{"version": 1, "run": "1", "revision": "%s", '
        '"environments": {"e": {"tests/test_a.py": [true, 1]}}}',
        '{"version": 1, "run": "1", "revision": "%s", '
        '"environments": {"e": {"tests/test_a.py": [1.5, 1]}}}',
        '{"version": 1, "run": "1", "revision": "%s", '
        '"environments": {"e": {"tests/test_a.py": [1, 1, 1]}}}',
    ],
)
def test_table_parser_refuses_malformed_tables(document: str) -> None:
    text = document.replace("%s", REVISION)

    with pytest.raises(PlanFailure):
        parsed_table(text)


def junit_text(cases: dict[str, float]) -> str:
    rows = "".join(
        f'<testcase classname="{name.rpartition(".")[0]}" '
        f'name="{name.rpartition(".")[2]}" time="{seconds}"/>'
        for name, seconds in cases.items()
    )
    return f"<testsuites><testsuite>{rows}</testsuite></testsuites>"


COLLECTED = [
    *module_nodes("tests/test_a.py", 2),
    "tests/test_b.py::test_value",
    "tests/plugins/storage/test_store.py::test_value",
]


def write_receipt(directory: Path, run: str = "77", requested: str = REVISION) -> None:
    (directory / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": run,
                "run_attempt": "1",
                "checkout_revision": REVISION,
                "requested_revision": requested,
            }
        )
    )


def recorded_lane(
    root: Path, lane: str, nodes: list[str], seconds: float, run: str = "77"
) -> None:
    directory = root / f"scaffold-tests-ubuntu-24.04-3.11.17-{lane}"
    directory.mkdir()
    write_receipt(directory, run)
    (directory / "child-1.exit").write_text("0\n")
    (directory / "complete.txt").write_text(
        "\n".join(COLLECTED) + f"\n\n{len(COLLECTED)} tests collected in 0.10s\n"
    )
    cases = {identity(node): seconds for node in nodes}
    (directory / "child-1.xml").write_text(junit_text(cases))


def test_refresh_records_milliseconds_and_tests_per_module(tmp_path: Path) -> None:
    recorded_lane(tmp_path, "general-1", module_nodes("tests/test_a.py", 2), 0.25)
    recorded_lane(tmp_path, "general-2", ["tests/test_b.py::test_value"], 1.5)
    storage = tmp_path / "scaffold-tests-ubuntu-24.04-3.11.17-storage"
    storage.mkdir()
    write_receipt(storage)
    (storage / "junit.xml").write_text(junit_text({"tests.plugins.storage.x": 9.0}))

    table = recorded_table(tmp_path, validated_collection)

    assert table == Table(
        "77",
        REVISION,
        {
            "ubuntu-24.04-3.11.17": {
                "tests/test_a.py": Record(500, 2),
                "tests/test_b.py": Record(1500, 1),
            }
        },
    )


GREEN_PUSH_RUN = "one green master push run"


@pytest.mark.parametrize(
    ("corruption", "failure", "match"),
    [
        ("foreign test", PlanFailure, "not in the collection"),
        ("two runs", PlanFailure, GREEN_PUSH_RUN),
        ("pull request merge", PlanFailure, GREEN_PUSH_RUN),
        ("failed child", PlanFailure, GREEN_PUSH_RUN),
        ("missing receipt", PlanFailure, "not a regular file"),
        ("document type", PlanFailure, "document type"),
        ("missing duration", PlanFailure, "finite duration"),
        ("unexpected entry", PlanFailure, "unexpected entry"),
        ("incomplete collection", EvidenceFailure, "collection is incomplete"),
    ],
)
def test_refresh_refuses_artifacts_outside_one_green_push_run(
    tmp_path: Path, corruption: str, failure: type[Exception], match: str
) -> None:
    recorded_lane(tmp_path, "general-1", module_nodes("tests/test_a.py", 2), 0.25)
    directory = tmp_path / "scaffold-tests-ubuntu-24.04-3.11.17-general-1"
    report = directory / "child-1.xml"
    if corruption == "foreign test":
        report.write_text(junit_text({"tests.test_other.test_value": 1.0}))
    elif corruption == "two runs":
        recorded_lane(tmp_path, "general-2", ["tests/test_b.py::test_value"], 1, "78")
    elif corruption == "pull request merge":
        write_receipt(directory, requested="d" * 40)
    elif corruption == "failed child":
        (directory / "child-1.exit").write_text("1\n")
    elif corruption == "missing receipt":
        (directory / "receipt.json").unlink()
    elif corruption == "document type":
        report.write_text('<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>')
    elif corruption == "missing duration":
        report.write_text('<testsuite><testcase classname="a" name="b"/></testsuite>')
    elif corruption == "unexpected entry":
        (tmp_path / "notes.txt").write_text("unexpected")
    else:
        (directory / "complete.txt").write_text("\n".join(COLLECTED) + "\n")

    with pytest.raises(failure, match=match):
        recorded_table(tmp_path, validated_collection)
