import pytest

from tests.measurements.cel_sink_process import READY, SinkProcessResult
from tests.measurements.harness import new_report
from tests.measurements.test_cel_evaluator_lifetime import (
    SinkPayload,
    sink_exit_gate,
    sink_gates,
)


@pytest.mark.parametrize(
    ("measured", "allow_equal", "exceeded"),
    [
        (9, True, False),
        (10, True, False),
        (11, True, True),
        (10, False, True),
        (-1, False, False),
    ],
)
def test_numerical_diagnostics_preserve_boundary_and_signed_observation(
    measured: int, allow_equal: bool, exceeded: bool
) -> None:
    report = new_report()

    report.diagnostic("value", measured, 10, allow_equal=allow_equal)

    assert report.environment == {
        "diagnostic_value_measured": measured,
        "diagnostic_value_reference": 10,
        "diagnostic_value_exceeded": exceeded,
    }
    assert report.gates == {}


def test_slow_diagnostic_shutdown_preserves_writer_and_drain_failures() -> None:
    report = new_report()
    payload: SinkPayload = {
        "stats": {
            "shutdown_seconds": 0.5,
            "writer_alive_at_exit": False,
            "drained": True,
        },
        "points": [],
        "retaining_paths": [],
    }

    sink_gates(report, "fixture", payload, False)

    assert report.environment["diagnostic_fixture_shutdown_seconds_exceeded"] is True
    assert report.gates == {"fixture_writer": True, "fixture_drained": True}
    payload["stats"]["writer_alive_at_exit"] = True
    with pytest.raises(AssertionError, match="writer"):
        sink_gates(report, "fixture", payload, False)


@pytest.mark.parametrize(
    ("whole", "cleanup", "passed"),
    [
        (1.5, 0.01, True),
        (1.5, 1.0, True),
        (1.5, 1.000001, False),
        (60.0, 0.01, True),
        (60.000001, 0.01, False),
    ],
)
def test_cel_sink_exit_gate_preserves_distinct_interval_boundaries(
    whole: float, cleanup: float, passed: bool
) -> None:
    report = new_report()
    result = SinkProcessResult(
        0, 0, b"{}", b"", READY, whole, cleanup, False, False, False
    )

    if passed:
        sink_exit_gate(report, "fixture", result)
    else:
        with pytest.raises(AssertionError, match="fixture_post_work_exit"):
            sink_exit_gate(report, "fixture", result)

    assert report.gates == {"fixture_post_work_exit": passed}
    assert (
        report.environment["diagnostic_fixture_complete_workload_exit_seconds_measured"]
        == whole
    )
    assert (
        report.environment[
            "diagnostic_fixture_complete_workload_exit_seconds_reference"
        ]
        == 1.0
    )
    assert (
        report.environment["diagnostic_fixture_complete_workload_exit_seconds_exceeded"]
        is True
    )
    assert report.environment["fixture_post_work_seconds"] == cleanup
