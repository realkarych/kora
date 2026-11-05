from tests.api.test_cel_pattern_validation import (
    assert_safe_output,
    completion_statuses,
)
from tests.cel_eval.children import ROOT, full_path, run_child


def test_wait_patterns_check_event_and_poll_fields() -> None:
    code = "from tests.api.wait_pattern_scenarios import run\nrun('inventory')"

    result = run_child(code, full_path(ROOT))

    assert_safe_output(result)
    assert completion_statuses(result) == ["complete"]
    assert "inputs.data.ok" not in result.stderr


def test_wait_patterns_refuse_partial_and_oversized_inventories() -> None:
    code = "from tests.api.wait_pattern_scenarios import run\nrun('refusals')"

    result = run_child(code, full_path(ROOT))

    assert_safe_output(result)
    assert completion_statuses(result) == [
        "capacity_exhausted",
        "invalid_inventory",
        "invalid_inventory",
        "capacity_exhausted",
        "complete",
    ]
    assert "private-schema" not in result.stderr


def test_wait_patterns_release_typed_state_before_native_delivery() -> None:
    code = "from tests.api.wait_pattern_scenarios import run\nrun('cancellation')"

    result = run_child(code, full_path(ROOT))

    assert_safe_output(result)
    assert completion_statuses(result) == []
