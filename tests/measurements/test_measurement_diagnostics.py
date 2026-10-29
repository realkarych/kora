import pytest

from tests.measurements.harness import new_report
from tests.measurements.test_cel_evaluator_lifetime import SinkPayload, sink_gates


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
