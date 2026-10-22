import json
import sys
from fractions import Fraction

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


def test_cel_cost_normalization_preserves_rational_brackets() -> None:
    from tests.measurements.cel_cost_calibration import normalized_sample

    elapsed = 960
    reference_before = 640
    reference_after = 1280

    normalized = normalized_sample(
        elapsed, 32, reference_before, reference_after, 32, Fraction(60)
    )

    assert normalized == Fraction(60)


@pytest.mark.parametrize("invalid", [0, -1, True])
def test_cel_cost_normalization_refuses_invalid_reference(invalid: int) -> None:
    from tests.measurements.cel_cost_calibration import normalized_sample

    with pytest.raises(ValueError, match="positive integer"):
        normalized_sample(960, 32, invalid, 1280, 32, Fraction(60))


def test_cel_cost_residuals_use_independent_brackets_before_subtraction() -> None:
    from tests.measurements.cel_cost_calibration import paired_residuals

    target: dict[str, object] = {
        "elapsed_ns": 960,
        "calls": 32,
        "reference_before_ns": 640,
        "reference_after_ns": 1280,
        "reference_calls": 32,
    }
    control: dict[str, object] = {
        "elapsed_ns": 320,
        "calls": 32,
        "reference_before_ns": 320,
        "reference_after_ns": 960,
        "reference_calls": 32,
    }

    residuals = paired_residuals([target] * 18, [control] * 18, Fraction(60))

    assert residuals == [Fraction(30)] * 18


def test_cel_cost_residuals_refuse_a_normalized_reversal() -> None:
    from tests.measurements.cel_cost_calibration import paired_residuals

    target: dict[str, object] = {
        "elapsed_ns": 96,
        "calls": 32,
        "reference_before_ns": 640,
        "reference_after_ns": 1280,
        "reference_calls": 32,
    }
    control: dict[str, object] = {
        "elapsed_ns": 64,
        "calls": 32,
        "reference_before_ns": 32,
        "reference_after_ns": 96,
        "reference_calls": 32,
    }

    with pytest.raises(ValueError, match="identifiable"):
        paired_residuals([target] * 18, [control] * 18, Fraction(60))


def test_cel_cost_fitting_uses_the_upper_slope_and_upward_headroom() -> None:
    from tests.measurements.cel_cost_calibration import fit_time_weight

    series = [
        [(1, Fraction(2)), (2, Fraction(6)), (4, Fraction(8)), (8, Fraction(16))],
        [(1, Fraction(1)), (2, Fraction(2)), (4, Fraction(4)), (8, Fraction(8))],
    ]

    fitted = fit_time_weight(series)

    assert fitted == 4_500_000


@pytest.mark.parametrize("residual", [Fraction(0), Fraction(-1)])
def test_cel_cost_fitting_refuses_nonpositive_residuals(residual: Fraction) -> None:
    from tests.measurements.cel_cost_calibration import fit_time_weight

    series = [[(1, Fraction(2)), (2, Fraction(4)), (4, residual), (8, Fraction(16))]]

    with pytest.raises(ValueError, match="identifiable"):
        fit_time_weight(series)


def test_cel_cost_fitting_requires_four_distinct_sizes() -> None:
    from tests.measurements.cel_cost_calibration import fit_time_weight

    series = [[(1, Fraction(2)), (2, Fraction(4)), (2, Fraction(8)), (8, Fraction(16))]]

    with pytest.raises(ValueError, match="four distinct"):
        fit_time_weight(series)


def test_cel_cost_memory_fit_keeps_fixed_and_variable_contributions_separate() -> None:
    from tests.measurements.cel_cost_calibration import fit_memory_weights

    controls = [100, 200]
    positive_models = [(10, 500), (20, 700), (1000, 200)]

    fixed, multiplier = fit_memory_weights(controls, positive_models)

    assert fixed == 300
    assert multiplier == 30


def test_cel_cost_memory_fit_refuses_a_zero_variable_denominator() -> None:
    from tests.measurements.cel_cost_calibration import fit_memory_weights

    with pytest.raises(ValueError, match="positive logical"):
        fit_memory_weights([100], [(0, 200)])


def test_cel_regex_table_fit_uses_all_five_compiled_size_boundaries() -> None:
    from tests.measurements.cel_cost_calibration import fit_pattern_unit

    accepted_counts = [2046, 2046, 2046, 2046, 2046]

    fitted = fit_pattern_unit(accepted_counts)

    assert fitted == 7688


@pytest.mark.parametrize(
    ("elapsed", "quadratic", "accepted"),
    [
        ([2, 4, 8, 16], False, True),
        ([1, 4, 16, 64], True, True),
        ([1, 1, 1, 1], False, True),
        ([1, 4, 16, 64], False, False),
        ([1, 8, 64, 512], True, False),
    ],
)
def test_cel_cost_complexity_gates_keep_flat_and_expected_growth(
    elapsed: list[int], quadratic: bool, accepted: bool
) -> None:
    from tests.measurements.cel_cost_calibration import complexity_of

    points = [
        (size, Fraction(value))
        for size, value in zip((1, 2, 4, 8), elapsed, strict=True)
    ]

    result = complexity_of(points, quadratic=quadratic)

    assert result["within_limits"] is accepted


def test_cel_cost_catalogue_results_agree_with_native_execution() -> None:
    from kora.cel_eval.binding import load_binding
    from kora.cel_eval.convert import BoundValues
    from tests.measurements.cel_cost_catalogue import operation_cases

    cases = operation_cases()
    binding = load_binding()
    identities: set[str] = set()

    for case in cases:
        checked = case.checked()
        control = case.control_checked()
        prepared = bind(checked, case.bindings, case.types)
        assert isinstance(prepared, BoundValues)
        result = binding.execute(binding.compile(checked), prepared.values)
        baseline = binding.execute(binding.compile(control), prepared.values)
        assert type(result) is type(case.expected)
        assert result == case.expected
        assert type(baseline) is type(case.control_expected)
        assert baseline == case.control_expected
        assert case.name not in identities
        identities.add(case.name)

    assert len(cases) == 145
    assert {case.coefficient for case in cases} == {
        "evaluation",
        "node",
        "iteration",
        "map_quadratic",
        "concat_byte",
        "concat_value",
        "compare_byte",
        "compare_value",
        "function_byte",
        "rendering_character",
    }


@pytest.mark.parametrize(
    "family",
    [
        "concat_string",
        "compile_rendering",
        "evaluation",
        "regex_atoms_escape_w",
        "regex_match_unicode_late_failure",
    ],
)
def test_cel_cost_bracketed_families_complete_on_real_children(family: str) -> None:
    from tests.measurements.cel_cost_calibration import (
        COST_PRELUDE,
        validate_cost_family,
    )

    result = run_process(
        [sys.executable, "-I", "-c", COST_PRELUDE, json.dumps(full_path(ROOT)), family]
    )

    report = validate_cost_family(result.stdout, family)

    assert result.code == 0
    assert not result.stderr
    assert not result.overflow
    assert not result.timed_out
    assert len(report["cases"]) == (1 if family == "evaluation" else 4)
    assert result.descriptors_after == result.descriptors_before
    replacements: list[tuple[str, object]] = [
        ("completion", "wrong"),
        ("family", "wrong"),
        ("reference", {}),
        ("cases", []),
        ("resources", {}),
    ]
    for field, replacement in replacements:
        invalid = dict(report)
        invalid[field] = replacement
        with pytest.raises(ValueError, match="cost"):
            validate_cost_family(json.dumps(invalid).encode(), family)


def test_cel_cost_catalogue_predictors_match_individual_estimator_charges() -> None:
    from dataclasses import replace

    from kora.cel import Budget, ValueBound, Variable
    from kora.cel.cost import estimate
    from tests.cel.test_cel_cost import ZERO_WEIGHTS
    from tests.measurements.cel_cost_catalogue import operation_cases
    from tests.measurements.cel_cost_workload import binding_statistics

    cases = operation_cases()
    budget = Budget(10**30, 10**30)

    for case in cases:
        checked = case.checked()
        control = case.control_checked()
        environment: dict[str, Variable] = {}
        for name, value in case.bindings.items():
            size, values, _ = binding_statistics({name: value})
            items = len(value) if isinstance(value, list | dict) else 0
            environment[name] = Variable(
                case.types[name], ValueBound(size, values, items, 16384)
            )
        weights = replace(ZERO_WEIGHTS, **{case.coefficient: 1})
        actual = estimate(
            checked.tree, len(checked.rendering), environment, weights, budget
        )
        baseline = estimate(
            control.tree, len(control.rendering), environment, weights, budget
        )
        predictor = (
            actual.steps
            if case.coefficient == "evaluation"
            else actual.compile_steps - baseline.compile_steps
            if case.coefficient == "rendering_character"
            else actual.steps - baseline.steps
        )
        assert predictor == case.units, case.name


def test_cel_cost_catalogue_constructs_only_declared_individual_recipes() -> None:
    from tests.measurements.cel_cost_catalogue import (
        OPERATION_IDENTITIES,
        operation_case,
    )

    assert len(OPERATION_IDENTITIES) == len(set(OPERATION_IDENTITIES)) == 145
    for family, size in (("unknown", 1), ("concat_string", 3), ("evaluation", 2)):
        with pytest.raises(ValueError, match="undeclared cost case"):
            operation_case(family, size)


def test_cel_cost_declared_bounds_cover_originals_without_converting_them() -> None:
    from kora.cel.cost import DATA_BYTES, DATA_VALUES
    from tests.measurements.cel_cost_catalogue import operation_cases
    from tests.measurements.cel_cost_workload import binding_statistics

    cases = operation_cases()

    for case in cases:
        environment = case.environment()
        for name, value in case.bindings.items():
            size, values, _ = binding_statistics({name: value})
            bound = environment[name].bound
            assert bound is not None
            assert size <= bound.bytes <= DATA_BYTES
            assert values <= bound.values <= DATA_VALUES


def test_cel_cold_memory_catalogue_has_fixed_and_positive_logical_cases() -> None:
    from dataclasses import replace

    from kora.cel import Budget
    from kora.cel.cost import estimate
    from tests.cel.test_cel_cost import ZERO_WEIGHTS
    from tests.measurements.cel_cost_catalogue import MEMORY_IDENTITIES, memory_case

    weights = replace(ZERO_WEIGHTS, memory_byte=1)
    logical: dict[str, int] = {}

    for family, size in MEMORY_IDENTITIES:
        case = memory_case(family, size)
        checked = case.checked()
        logical[case.name] = estimate(
            checked.tree,
            len(checked.rendering),
            case.environment(),
            weights,
            Budget(10**30, 10**30),
        ).memory

    assert len(logical) == len(MEMORY_IDENTITIES)
    assert sum(value == 0 for value in logical.values()) == 13
    assert all(value > 0 for name, value in logical.items() if "control" not in name)
    assert max(logical.values()) >= 1_000_000


def test_cel_cold_memory_recipes_have_exact_native_results_and_valid_data() -> None:
    from kora.cel_eval.binding import load_binding
    from kora.cel_eval.convert import BoundValues
    from tests.measurements.cel_cost_catalogue import MEMORY_IDENTITIES, memory_case
    from tests.measurements.cel_cost_workload import binding_statistics

    binding = load_binding()
    cases = tuple(memory_case(family, size) for family, size in MEMORY_IDENTITIES)

    for case in cases:
        checked = case.checked()
        prepared = bind(checked, case.bindings, case.types)
        assert isinstance(prepared, BoundValues), case.name
        result = binding.execute(binding.compile(checked), prepared.values)
        original = binding_statistics(case.bindings)
        converted = binding_statistics(prepared.values)
        assert type(result) is type(case.expected), case.name
        assert result == case.expected, case.name
        assert max(original[0], converted[0]) <= 4096, case.name
        assert max(original[1], converted[1]) <= 256, case.name
        assert max(original[2], converted[2]) <= 32, case.name


def test_cel_cold_memory_depth_control_retains_native_conditional_depth() -> None:
    from kora.cel.tree import Conditional
    from tests.measurements.cel_cost_catalogue import memory_case

    case = memory_case("memory_control_depth", 32)

    checked = case.checked()
    node = checked.tree
    depth = 0
    while isinstance(node, Conditional):
        depth += 1
        node = node.otherwise

    assert depth == 32
    assert checked.rendering.count("?") == 32


def test_cel_cold_memory_source_control_reaches_the_exact_source_limit() -> None:
    from tests.measurements.cel_cost_catalogue import memory_case

    case = memory_case("memory_control_arithmetic_tree", 1024)

    checked = case.checked()

    assert len(case.source) == 4096
    assert len(checked.rendering) == 6146


@pytest.mark.parametrize("family", ["convert_bytes", "convert_values"])
def test_cel_conversion_axes_keep_the_other_dimension_constant(family: str) -> None:
    from tests.measurements.cel_cost_catalogue import axis_cases
    from tests.measurements.cel_cost_workload import binding_statistics

    cases = axis_cases(family)

    observed = [binding_statistics(case.bindings) for case in cases]
    controls = [binding_statistics(case.control) for case in cases]

    assert len(cases) == 4
    if family == "convert_bytes":
        assert [point[0] for point in observed] == [512, 1024, 2048, 4096]
        assert {point[1] for point in observed + controls} == {1}
        assert {point[0] for point in controls} == {2}
    else:
        assert [point[1] for point in observed] == [16, 32, 64, 128]
        assert {point[0] for point in observed + controls} == {4096}
        assert {point[1] for point in controls} == {2}


@pytest.mark.parametrize("family", ["convert_bytes", "convert_values"])
@pytest.mark.parametrize("mode", ["prepare", "native"])
def test_cel_conversion_axis_children_retain_complete_brackets(
    family: str, mode: str
) -> None:
    from tests.measurements.cel_cost_calibration import (
        AXIS_PRELUDE,
        mapping,
        sequence,
        validate_axis_family,
    )

    result = run_process(
        [
            sys.executable,
            "-I",
            "-c",
            AXIS_PRELUDE,
            json.dumps(full_path(ROOT)),
            family,
            mode,
        ]
    )

    report = validate_axis_family(result.stdout, family, mode)

    assert result.code == 0
    assert not result.stderr
    assert not result.timed_out
    assert not result.overflow
    assert result.descriptors_before == result.descriptors_after
    entries = sequence(report["cases"])
    assert len(entries) == 4
    invalid = dict(report)
    observations = [dict(mapping(item)) for item in entries]
    observations[0]["predictor_units"] = True
    invalid["cases"] = observations
    with pytest.raises(ValueError, match="axis"):
        validate_axis_family(json.dumps(invalid).encode(), family, mode)
    invalid["resources"] = {}
    with pytest.raises(ValueError, match="axis"):
        validate_axis_family(json.dumps(invalid).encode(), family, mode)


@pytest.mark.parametrize(
    ("family", "size"),
    [
        ("memory_control_true", 1),
        ("memory_intermediate", 32),
        ("regex_atoms_escape_w", 8),
    ],
)
def test_cel_cold_memory_child_measures_before_oracle_and_releases(
    family: str, size: int
) -> None:
    from tests.measurements.cel_cost_calibration import (
        MEMORY_PRELUDE,
        validate_memory_observation,
    )

    result = run_process(
        [
            sys.executable,
            "-I",
            "-c",
            MEMORY_PRELUDE,
            json.dumps(full_path(ROOT)),
            family,
            str(size),
        ]
    )

    report = validate_memory_observation(result.stdout, family, size)

    assert result.code == 0
    assert not result.stderr
    assert not result.overflow
    assert not result.timed_out
    assert report["completion"] == "cel-cold-memory-complete"
    assert report["linux_peak_reset"] is (sys.platform == "linux")
    assert result.descriptors_before == result.descriptors_after
    invalid = dict(report)
    invalid["resources"] = {}
    with pytest.raises(ValueError, match="memory"):
        validate_memory_observation(json.dumps(invalid).encode(), family, size)
    impossible = dict(report)
    impossible["linux_peak_reset"] = True
    for point in ("baseline", "reset", "measured"):
        impossible[point] = {"current": 1024, "peak": 512}
    with pytest.raises(ValueError, match="memory"):
        validate_memory_observation(json.dumps(impossible).encode(), family, size)
