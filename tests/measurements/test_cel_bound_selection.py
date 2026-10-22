import json
import sys
from pathlib import Path

import pytest

from tests.cel_eval.children import ROOT, full_path, isolated_path, isolated_site
from tests.measurements.cel_bound_workload import (
    BOUND_FAMILIES,
    BOUND_PAIRS,
    bound_case,
    conversion_samples,
    select_bounds,
)
from tests.measurements.cel_cost_calibration import (
    PRELUDE,
    mapping,
    validate_observation,
)
from tests.measurements.cel_measurement_process import run_process


def test_cel_bound_selection_uses_headroom_and_stable_order() -> None:
    passing = {(1024, 512), (2048, 256), (4096, 128)}
    minima = {
        (size, count, family): 1_666_666 if (size, count) in passing else 1_666_667
        for size, count in BOUND_PAIRS
        for family in BOUND_FAMILIES
    }

    selected = select_bounds(minima)

    assert selected == (4096, 128)
    minima[4096, 128, "mixed"] = 1_666_667
    assert select_bounds(minima) == (2048, 256)
    assert select_bounds({key: 1_666_667 for key in minima}) is None


@pytest.mark.parametrize("control", [33, 63, 64])
def test_cel_bound_selection_refuses_nonpositive_raw_residuals(control: int) -> None:
    preparation = [100] * 18
    native_totals = [96] * 3 + [33] * 15
    control_totals = [96] * 3 + [control] * 15

    with pytest.raises(ValueError, match="raw native"):
        conversion_samples(preparation, native_totals, control_totals)


def test_cel_bound_selection_preserves_paired_samples_and_rounding() -> None:
    preparation = list(range(100, 118))
    native_totals = [64 + 32 * i for i in range(18)]
    control_totals = [33 + 32 * i for i in range(18)]

    combined = conversion_samples(preparation, native_totals, control_totals)

    assert combined == list(range(101, 119))


def test_cel_bound_selection_requires_the_complete_closed_manifest() -> None:
    minima = {
        (size, count, family): 100
        for size, count in BOUND_PAIRS
        for family in BOUND_FAMILIES
    }
    del minima[1024, 128, "ascii"]

    with pytest.raises(ValueError, match="manifest"):
        select_bounds(minima)

    minima[1024, 128, "unknown"] = 100
    with pytest.raises(ValueError, match="manifest"):
        select_bounds(minima)


@pytest.mark.parametrize(
    ("family", "original_bytes", "converted_bytes", "values", "depth"),
    [
        ("ascii", 1024, 1024, 5, 1),
        ("unicode", 1021, 1021, 5, 1),
        ("escapes", 1021, 1021, 5, 1),
        ("empty_lists", 382, 382, 128, 1),
        ("small_maps", 993, 993, 113, 2),
        ("deep_chains", 259, 259, 128, 32),
        ("numbers", 287, 523, 128, 2),
        ("key_heavy", 1024, 1024, 87, 2),
        ("mixed", 1024, 1024, 128, 32),
        ("many_variables", 1024, 1024, 128, 1),
    ],
)
def test_cel_bound_catalogue_has_independent_small_corner_expectations(
    family: str, original_bytes: int, converted_bytes: int, values: int, depth: int
) -> None:
    case = bound_case(1024, 128, family)

    metadata = case.metadata()
    serialized = sum(
        len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        for value in case.bindings.values()
    )

    assert serialized == original_bytes
    assert metadata["original_bytes"] == original_bytes
    assert metadata["converted_bytes"] == converted_bytes
    assert metadata["original_values"] == values
    assert metadata["values"] == values
    assert metadata["original_depth"] == depth
    assert metadata["converted_depth"] == depth
    assert metadata["limiting_constraints"]


def test_cel_bound_catalogue_is_fixed_and_satisfies_both_candidate_caps() -> None:
    identities: set[str] = set()

    for size, count in BOUND_PAIRS:
        for family in BOUND_FAMILIES:
            case = bound_case(size, count, family)
            metadata = case.metadata()
            assert case.name not in identities
            identities.add(case.name)
            assert max(metadata["original_bytes"], metadata["converted_bytes"]) <= size
            assert max(metadata["original_values"], metadata["values"]) <= count
            assert metadata["original_depth"] <= 32
            assert metadata["converted_depth"] <= 32

    assert len(identities) == 120


@pytest.mark.parametrize("mode", ["full", "native"])
@pytest.mark.parametrize("family", ["ascii", "many_variables"])
def test_cel_bound_observations_cover_real_single_and_multiple_roots(
    mode: str, family: str
) -> None:
    case = bound_case(1024, 128, family)

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
    calls = mapping(observation["calls"])

    assert result.code == 0
    assert not result.stderr
    assert not result.timed_out
    assert not result.overflow
    assert calls == (
        {"native_ns": 32, "control_ns": 32}
        if mode == "native"
        else {name: 1 for name in calls}
    )
    for name, replacement in (
        ("calls", {"native_ns": 31, "control_ns": 32}),
        ("converted_sha256", "wrong"),
        ("control_sha256", "wrong"),
        ("variable_types", {}),
        ("limiting_constraints", ["wrong"]),
        ("byte_limit", True),
    ):
        altered = dict(observation)
        altered[name] = replacement
        with pytest.raises(ValueError, match="observation"):
            validate_observation(json.dumps(altered).encode(), case, mode)
    repeated = b'{"schema":2,' + result.stdout[1:]
    with pytest.raises(ValueError, match="duplicate"):
        validate_observation(repeated, case, mode)


@pytest.mark.parametrize("missing_wheel", [False, True])
def test_cel_bound_collection_retains_initialization_and_reference_failures(
    tmp_path: Path, missing_wheel: bool
) -> None:
    search_path = (
        isolated_path(isolated_site(tmp_path / "site"), ROOT)
        if missing_wheel
        else full_path(ROOT)
    )
    output = tmp_path / "failed.json"
    code = (
        "import json, sys; from pathlib import Path; "
        "sys.path[:] = json.loads(sys.argv[1]); "
        "from tests.measurements.cel_cost_calibration import collect_conversion; "
        "raise SystemExit(collect_conversion(Path(sys.argv[2]), 'bounds'))"
    )

    result = run_process(
        [sys.executable, "-I", "-c", code, json.dumps(search_path), str(output)]
    )
    report = mapping(json.loads(output.read_text()))

    assert result.code != 0
    assert not result.timed_out
    assert not result.overflow
    assert isinstance(report, dict)
    assert report["status"] == "measurement_failed"
    assert report["selected_bounds"] is None
    assert report["phase"] == (
        "provenance" if missing_wheel else "reference_qualification"
    )
    assert report["failures"]
    assert not report["observations"]
