import json

from kora.api.cel_pattern_validation import COMPLETED
from tests.api.cel_pattern_validation_scenarios import (
    PATH_MARKER,
    PATTERN_MARKER,
    REQUEST_ID,
)
from tests.cel_eval.children import ROOT, ChildResult, full_path, run_child


def run_inventory_scenario(name: str) -> ChildResult:
    code = f"from tests.api.cel_pattern_validation_scenarios import run\nrun({name!r})"
    return run_child(code, full_path(ROOT))


def completion_statuses(result: ChildResult) -> list[object]:
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    completions = [record for record in records if record["event"] == COMPLETED.name]
    assert all(record["request_id"] == REQUEST_ID for record in completions)
    assert all(record["level"] == "DEBUG" for record in completions)
    return [record["status"] for record in completions]


def assert_safe_output(result: ChildResult) -> None:
    assert result.code == 0, result.stderr
    assert result.stdout == ""
    assert PATTERN_MARKER not in result.stderr
    assert PATH_MARKER not in result.stderr
    assert "Function 'matches' error:" not in result.stderr


def test_cel_pattern_inventory_checks_all_sites_and_deduplicates() -> None:
    scenario = "deduplication"

    result = run_inventory_scenario(scenario)

    assert_safe_output(result)
    assert completion_statuses(result) == ["complete", "complete", "complete"]


def test_cel_pattern_inventory_refuses_incomplete_or_oversized_inputs() -> None:
    scenario = "refusals"

    result = run_inventory_scenario(scenario)

    assert_safe_output(result)
    assert completion_statuses(result) == [
        "capacity_exhausted",
        "invalid_inventory",
        "invalid_inventory",
        "invalid_inventory",
        "invalid_inventory",
        "complete",
        "generated_rejected",
    ]


def test_cel_pattern_inventory_cancels_and_releases_probe_work() -> None:
    scenario = "cancellation"

    result = run_inventory_scenario(scenario)

    assert_safe_output(result)
    assert completion_statuses(result) == []
