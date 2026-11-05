import json

import pytest

from tests.api.cel_pattern_validation_scenarios import PATTERN_MARKER, REQUEST_ID
from tests.api.predicate_rendering_scenarios import EVENTS, FAMILIES, Family
from tests.cel_eval.children import ROOT, ChildResult, full_path, run_child

Results = tuple[Family, dict[str, ChildResult]]


@pytest.fixture(scope="module", params=FAMILIES)
def rendering_results(request: pytest.FixtureRequest) -> Results:
    value: object = request.param
    assert isinstance(value, str)
    assert value in FAMILIES
    family = value
    return family, {
        scenario: run_child(
            "from tests.api.predicate_rendering_scenarios import run\n"
            f"run({family!r}, {scenario!r})",
            full_path(ROOT),
        )
        for scenario in ("inventory", "refusals", "failures", "cancellation")
    }


def completion_statuses(family: Family, result: ChildResult) -> list[object]:
    assert result.code == 0, result.stderr
    assert result.stdout == ""
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    names = {event.name for event in EVENTS.values()}
    completed = [entry for entry in records if entry["event"] in names]
    assert all(entry["event"] == EVENTS[family].name for entry in completed)
    assert all(entry["request_id"] == REQUEST_ID for entry in completed)
    assert all(entry["level"] == "DEBUG" for entry in completed)
    return [entry["status"] for entry in completed]


def test_predicate_renderings_compile_complete_scoped_inventories(
    rendering_results: Results,
) -> None:
    family, results = rendering_results

    statuses = completion_statuses(family, results["inventory"])

    assert statuses == ["compiled", "compiled"]


def test_predicate_renderings_admit_before_native_work(
    rendering_results: Results,
) -> None:
    family, results = rendering_results

    statuses = completion_statuses(family, results["refusals"])

    assert statuses == [
        "capacity_exhausted",
        *["invalid_allowance"] * 8,
        "capacity_exhausted",
        *["typed_refused"] * (2 if family == "sensor" else 4),
        "compiled",
    ]


def test_predicate_renderings_associate_native_refusal(
    rendering_results: Results,
) -> None:
    family, results = rendering_results

    statuses = completion_statuses(family, results["failures"])

    assert statuses == ["rendering_refused", "evaluator_unavailable"]


def test_predicate_renderings_release_cancelled_and_failed_work(
    rendering_results: Results,
) -> None:
    family, results = rendering_results

    statuses = completion_statuses(family, results["cancellation"])

    assert statuses == ["compiled"]


def test_predicate_renderings_report_content_free_statuses(
    rendering_results: Results,
) -> None:
    family, results = rendering_results
    forbidden = (
        PATTERN_MARKER,
        "private-schema",
        "inputs.data.ok",
        "params.ok",
        "payload.ready",
        "observation.ready",
        "graph.nodes",
        "graph.triggers",
    )

    statuses = {
        name: completion_statuses(family, result) for name, result in results.items()
    }

    assert all(statuses.values())
    assert all(
        content not in result.stderr
        for result in results.values()
        for content in forbidden
    )
