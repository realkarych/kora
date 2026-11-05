import json

import pytest

from kora.api.branch_renderings import COMPLETED
from tests.api.cel_pattern_validation_scenarios import PATTERN_MARKER, REQUEST_ID
from tests.cel_eval.children import ROOT, ChildResult, full_path, run_child


@pytest.fixture(scope="module")
def rendering_results() -> dict[str, ChildResult]:
    return {
        scenario: run_child(
            f"from tests.api.branch_rendering_scenarios import run\nrun({scenario!r})",
            full_path(ROOT),
        )
        for scenario in ("inventory", "refusals", "failures", "cancellation")
    }


def completion_statuses(result: ChildResult) -> list[object]:
    assert result.code == 0, result.stderr
    assert result.stdout == ""
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    completed = [entry for entry in records if entry["event"] == COMPLETED.name]
    assert all(entry["request_id"] == REQUEST_ID for entry in completed)
    assert all(entry["level"] == "DEBUG" for entry in completed)
    return [entry["status"] for entry in completed]


def test_branch_renderings_compile_conditions_and_dormant_reasons(
    rendering_results: dict[str, ChildResult],
) -> None:
    result = rendering_results["inventory"]

    statuses = completion_statuses(result)

    assert statuses == ["compiled", "compiled"]


def test_branch_renderings_refuse_partial_and_over_capacity_inventories(
    rendering_results: dict[str, ChildResult],
) -> None:
    result = rendering_results["refusals"]

    statuses = completion_statuses(result)

    assert statuses == [
        "capacity_exhausted",
        "invalid_allowance",
        "invalid_allowance",
        "invalid_allowance",
        "invalid_allowance",
        "capacity_exhausted",
        "typed_refused",
        "typed_refused",
        "typed_refused",
        "typed_refused",
        "compiled",
    ]


def test_branch_renderings_preserve_native_failure_association(
    rendering_results: dict[str, ChildResult],
) -> None:
    result = rendering_results["failures"]

    statuses = completion_statuses(result)

    assert statuses == ["rendering_refused", "evaluator_unavailable"]


def test_branch_renderings_cancel_and_release_pending_work(
    rendering_results: dict[str, ChildResult],
) -> None:
    result = rendering_results["cancellation"]

    statuses = completion_statuses(result)

    assert statuses == ["compiled"]


def test_branch_renderings_keep_completion_diagnostics_content_free(
    rendering_results: dict[str, ChildResult],
) -> None:
    forbidden = (
        PATTERN_MARKER,
        "private-schema",
        "inputs.data.ok",
        "params.ok",
        "cases",
    )

    statuses = {
        scenario: completion_statuses(result)
        for scenario, result in rendering_results.items()
    }

    assert all(statuses.values())
    assert all(
        content not in result.stderr
        for result in rendering_results.values()
        for content in forbidden
    )
