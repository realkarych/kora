import json
import sys

import pytest

from kora.cel_eval.convert import bind
from kora.cel_eval.errors import ErrorKind
from tests.cel_eval.children import full_path
from tests.measurements.cel_bound_workload import bound_case
from tests.measurements.cel_cost_calibration import (
    PRELUDE,
    ROOT,
    timings_of,
    validate_observation,
)
from tests.measurements.cel_cost_workload import pilot_cases, value_depth
from tests.measurements.cel_measurement_process import run_process


@pytest.mark.parametrize(
    ("family", "original_bytes", "converted_bytes", "values", "depth"),
    [
        ("ascii", 65517, 65517, 5, 1),
        ("unicode", 65421, 65421, 5, 1),
        ("escapes", 65485, 65485, 5, 1),
        ("empty_lists", 65001, 65001, 22001, 2),
        ("small_maps", 62001, 62001, 7001, 2),
        ("deep_chains", 65001, 65001, 32001, 32),
        ("numbers", 34001, 62001, 15001, 2),
    ],
)
def test_cel_conversion_pilot_preserves_and_refuses_historical_shapes(
    family: str, original_bytes: int, converted_bytes: int, values: int, depth: int
) -> None:
    case = next(c for c in pilot_cases() if c.family == family and c.scale == 8)
    checked = case.checked()
    metadata = case.workload().metadata()

    converted = bind(checked, {"values": case.value}, {"values": case.type})

    assert case.original_size().bytes == original_bytes
    assert converted is ErrorKind.BOUND_EXCEEDED
    assert metadata["converted_bytes"] == converted_bytes
    assert metadata["values"] == values
    assert value_depth(case.value) == depth
    assert metadata["converted_depth"] == depth


def test_cel_conversion_pilot_has_four_sizes_per_family() -> None:
    cases = pilot_cases()

    families = {case.family for case in cases}

    assert len(cases) == 28
    assert len(families) == 7
    assert all(
        [case.scale for case in cases if case.family == family] == [1, 2, 4, 8]
        for family in families
    )


@pytest.mark.parametrize("mode", ["full", "native"])
def test_cel_conversion_observations_come_from_completed_real_children(
    mode: str,
) -> None:
    case = bound_case(4096, 256, "ascii")

    result = run_process(
        [
            sys.executable,
            "-I",
            "-c",
            PRELUDE,
            json.dumps(full_path(ROOT)),
            case.name,
            mode,
            "bounds",
        ]
    )
    observation = validate_observation(result.stdout, case, mode)

    assert result.code == 0
    assert not result.stderr
    assert not result.timed_out
    assert not result.overflow
    assert all(len(values) == 18 for values in timings_of(observation).values())
    for name, replacement in (
        ("case", "wrong"),
        ("input_sha256", "wrong"),
        ("completion", "wrong"),
        ("original_depth", 33),
        ("converted_depth", True),
        ("stack_restored", False),
        ("timings", {"native_ns": [True] * 18}),
        ("resources", {"handles": 1}),
    ):
        altered = dict(observation)
        altered[name] = replacement
        with pytest.raises(ValueError, match="observation|timing"):
            validate_observation(json.dumps(altered).encode(), case, mode)
