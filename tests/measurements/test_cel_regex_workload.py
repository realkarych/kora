import json
import sys

import pytest

from kora.cel.regex import CLASS_WEIGHTS, UNICODE_CLASS_WEIGHTS
from kora.cel_eval.binding import load_binding
from kora.cel_eval.convert import BoundValues, bind
from tests.cel_eval.children import ROOT, full_path
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.cel_regex_workload import (
    BOUNDARY_PRELUDE,
    MATCH_PATTERNS,
    PATTERN_FORMS,
    REGEX_IDENTITIES,
    boundary_limits,
    regex_case,
    validate_boundary,
)


def test_regex_catalogue_covers_every_table_entry_and_exact_native_result() -> None:
    binding = load_binding()
    rows = {row for form in PATTERN_FORMS for row, _ in form.terms}
    cases = tuple(regex_case(family, size) for family, size in REGEX_IDENTITIES)

    for case in cases:
        checked = case.checked()
        control = case.control_checked()
        values = bind(checked, case.bindings, case.types)
        assert isinstance(values, BoundValues), case.name
        result = binding.execute(binding.compile(checked), values.values)
        baseline = binding.execute(binding.compile(control), values.values)
        assert type(result) is type(case.expected), case.name
        assert result == case.expected, case.name
        assert type(baseline) is type(case.control_expected), case.name
        assert baseline == case.control_expected, case.name

    assert {"class:" + name for name in CLASS_WEIGHTS} <= rows
    assert {"unicode:" + name for name in UNICODE_CLASS_WEIGHTS} <= rows
    assert len({case.name for case in cases}) == len(REGEX_IDENTITIES)
    assert {
        "literal",
        "caseless_literal",
        "range",
        "caseless_range",
        "any",
        "anchor",
    } <= rows


@pytest.mark.parametrize("form", ["literal_ascii", "anchor_start"])
def test_regex_boundary_children_report_adjacent_limit_or_censoring(form: str) -> None:
    result = run_process(
        [
            sys.executable,
            "-I",
            "-c",
            BOUNDARY_PRELUDE,
            json.dumps(full_path(ROOT)),
            form,
        ]
    )

    report = validate_boundary(result.stdout, form)
    accepted, refused = boundary_limits(report["attempts"])

    assert result.code == 0
    assert not result.stderr
    assert not result.timed_out
    assert not result.overflow
    assert result.descriptors_before == result.descriptors_after
    assert 2 <= len(report["attempts"]) <= 22
    if form == "literal_ascii":
        assert refused is not None
        assert refused == accepted + 1
    else:
        assert accepted == 1_048_576
        assert refused is None
    invalid = dict(report)
    invalid["attempts"] = []
    with pytest.raises(ValueError, match="boundary"):
        validate_boundary(json.dumps(invalid).encode(), form)
    for field, replacement in (
        ("count", True),
        ("status", "unknown"),
        ("source_sha256", "wrong"),
        ("result_sha256", "wrong"),
        ("error", "unexpected native failure"),
    ):
        altered: dict[str, object] = dict(report)
        attempts: list[dict[str, object]] = [dict(item) for item in report["attempts"]]
        attempts[0][field] = replacement
        altered["attempts"] = attempts
        with pytest.raises(ValueError, match="boundary"):
            validate_boundary(json.dumps(altered).encode(), form)


def test_regex_boundary_trace_refuses_missing_bisections_and_early_refusal() -> None:
    from tests.measurements.cel_regex_workload import BoundaryAttempt

    def attempt(count: int, status: str) -> BoundaryAttempt:
        return BoundaryAttempt(
            count=count,
            status=status,
            source_sha256="unused",
            result_sha256=None,
            error=None,
        )

    invalid = (
        [attempt(1, "refused"), attempt(1_048_576, "refused")],
        [attempt(1, "accepted"), attempt(1_048_576, "refused")],
        [
            attempt(1, "accepted"),
            attempt(1_048_576, "accepted"),
            attempt(2, "accepted"),
        ],
        [attempt(1, "accepted"), attempt(1_048_576, "refused"), attempt(2, "accepted")],
    )

    for trace in invalid:
        with pytest.raises(ValueError, match="boundary"):
            boundary_limits(trace)
    converged = [attempt(1, "accepted"), attempt(1_048_576, "refused")]
    upper = 524_288
    while upper > 1:
        converged.append(attempt(upper, "refused"))
        upper //= 2
    after_convergence = [*converged, attempt(1, "accepted")]
    with pytest.raises(ValueError, match="boundary"):
        boundary_limits(after_convergence)


def test_regex_matching_patterns_accept_the_complete_valid_sized_subject() -> None:
    binding = load_binding()

    for name, pattern, character in MATCH_PATTERNS:
        case = regex_case("regex_match_" + name + "_late_failure", 512)
        values = {"text": character * (512 if character == "a" else 128)}
        result = binding.execute(binding.compile(case.checked()), values)
        assert result is False, name
        assert pattern.startswith("^"), name
