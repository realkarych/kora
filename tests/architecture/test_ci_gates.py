import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts/ci/require_outcomes.sh"
OUTCOMES = ("success", "failure", "cancelled", "skipped", "true", "false", "")
REQUIREMENTS = {
    "FIRST_OUTCOME": "success",
    "SECOND_RESULT": "success",
    "PROOF_VERIFIED": "true",
    "WORKFLOW_CANCELLED": "false",
}
COMPLETION_FIELDS = (
    "DEPENDENCIES_OUTCOME",
    "PREPARATION_OUTCOME",
    "TEARDOWN_OUTCOME",
    "FRONTEND_OUTCOME",
    "STATIC_OUTCOME",
    "TESTS_OUTCOME",
    "UPLOAD_OUTCOME",
    "DISTRIBUTIONS_OUTCOME",
    "FINAL_SYNC_OUTCOME",
)
WITHOUT_FIXTURES = {"PREPARATION_OUTCOME": "skipped", "TEARDOWN_OUTCOME": "skipped"}
WITHOUT_BUILDS = {"STATIC_OUTCOME": "skipped", "DISTRIBUTIONS_OUTCOME": "skipped"}
LANE_OUTCOMES = {
    "general-1": {**WITHOUT_FIXTURES, **WITHOUT_BUILDS},
    "general-8": {**WITHOUT_FIXTURES, **WITHOUT_BUILDS},
    "storage": {**WITHOUT_FIXTURES, **WITHOUT_BUILDS},
    "local-runtime": {},
}
UNPLANNED_LANES = ("general-0", "general-9", "general-10", "general-", "conformance")
SCAFFOLD_JOIN_FIELDS = (
    "PLAN_RESULT",
    "LANES_RESULT",
    "MACOS_LANES_RESULT",
    "DEPENDENCIES_OUTCOME",
    "DOWNLOAD_OUTCOME",
    "VERIFICATION_OUTCOME",
)
SCOPE = {"SCOPE_RESULT": "success", "SCOPE_VERIFIED": "true"}
CEL_SUCCESS = {
    **SCOPE,
    "FULL_SCOPE": "true",
    "SCAFFOLD_RESULT": "success",
    "SCAFFOLD_VERIFIED": "true",
    "CEL_RESULT": "success",
    "CEL_VERIFIED": "true",
    "SOURCE_REVISION_OUTCOME": "success",
    "CEL_LIFETIME_DOWNLOAD_OUTCOME": "success",
    "CEL_COST_DOWNLOAD_OUTCOME": "success",
    "CEL_INVENTORY_OUTCOME": "success",
    "WORKFLOW_CANCELLED": "false",
}
EDITORIAL_INPUTS = {
    "scaffold-evidence": (
        *SCOPE,
        "FULL_SCOPE",
        *SCAFFOLD_JOIN_FIELDS,
        "WORKFLOW_CANCELLED",
    ),
    "cel-measurements": tuple(CEL_SUCCESS),
    "browser": (*SCOPE, "FULL_SCOPE", "BROWSER_RESULT", "CANCELLED"),
}
GateCase = tuple[str, dict[str, str], bool]


def run_gate(
    arguments: list[str], values: dict[str, str | None], output: Path
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        name: value for name, value in os.environ.items() if name not in values
    }
    environment.update(
        {name: value for name, value in values.items() if value is not None}
    )
    environment["GITHUB_OUTPUT"] = str(output)
    return subprocess.run(
        ["/bin/bash", str(GATES), *arguments],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )


def recorded(output: Path) -> str:
    return output.read_text() if output.exists() else ""


def requirement_arguments() -> list[str]:
    return [f"{name}={value}" for name, value in REQUIREMENTS.items()]


@pytest.mark.parametrize("mode", ["check", "verify"])
def test_outcome_gate_accepts_complete_success(tmp_path: Path, mode: str) -> None:
    output = tmp_path / "output"

    result = run_gate([mode, *requirement_arguments()], {**REQUIREMENTS}, output)

    assert result.returncode == 0, result.stderr
    assert recorded(output) == ("verified=true\n" if mode == "verify" else "")


@pytest.mark.parametrize("mode", ["check", "verify"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field, expected in REQUIREMENTS.items()
        for value in (*OUTCOMES, None)
        if value != expected
    ],
)
def test_outcome_gate_refuses_every_other_or_missing_outcome(
    tmp_path: Path, mode: str, field: str, value: str | None
) -> None:
    output = tmp_path / "output"

    result = run_gate(
        [mode, *requirement_arguments()], {**REQUIREMENTS, field: value}, output
    )

    assert result.returncode != 0
    assert recorded(output) == ""


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["check"],
        ["verify"],
        ["check", "FIRST_OUTCOME"],
        ["check", "=success"],
        ["check", "first_outcome=success"],
        ["check", "FIRST-OUTCOME=success"],
        ["require", "FIRST_OUTCOME=success"],
        ["browser", "FIRST_OUTCOME=success"],
    ],
)
def test_outcome_gate_refuses_malformed_requirements(
    tmp_path: Path, arguments: list[str]
) -> None:
    output = tmp_path / "output"
    values: dict[str, str | None] = {
        **REQUIREMENTS,
        "first_outcome": "success",
        "BROWSER_RESULT": "success",
        "FULL_SCOPE": "true",
        "CANCELLED": "false",
        **SCOPE,
    }

    result = run_gate(arguments, values, output)

    assert result.returncode != 0
    assert recorded(output) == ""


def expected_lane_outcomes(lane: str) -> dict[str, str]:
    return {**dict.fromkeys(COMPLETION_FIELDS, "success"), **LANE_OUTCOMES[lane]}


def lane_completion_cases() -> dict[str, GateCase]:
    cases: dict[str, GateCase] = {}
    for lane in LANE_OUTCOMES:
        complete = expected_lane_outcomes(lane)
        cases[f"{lane}-complete"] = (lane, complete, True)
        cases[f"{lane}-workflow-cancelled"] = (
            lane,
            {**complete, "WORKFLOW_CANCELLED": "true"},
            False,
        )
        for field, expected in complete.items():
            for outcome in ("success", "failure", "cancelled", "skipped", ""):
                if outcome != expected:
                    cases[f"{lane}-{field}-{outcome or 'empty'}"] = (
                        lane,
                        {**complete, field: outcome},
                        False,
                    )
    for lane in UNPLANNED_LANES:
        cases[f"unplanned-{lane}-profile"] = (
            lane,
            expected_lane_outcomes("general-1"),
            False,
        )
    cases["unknown-local-runtime-profile"] = (
        "unknown",
        expected_lane_outcomes("local-runtime"),
        False,
    )
    cases["empty-general-profile"] = (
        "",
        expected_lane_outcomes("general-1"),
        False,
    )
    return cases


LANE_COMPLETION_CASES = lane_completion_cases()


@pytest.mark.parametrize(
    ("lane", "outcomes", "passes"),
    list(LANE_COMPLETION_CASES.values()),
    ids=list(LANE_COMPLETION_CASES),
)
def test_lane_completion_rejects_every_incomplete_outcome(
    tmp_path: Path, lane: str, outcomes: dict[str, str], passes: bool
) -> None:
    values: dict[str, str | None] = {
        "WORKFLOW_CANCELLED": "false",
        **outcomes,
        "KORA_SCAFFOLD_LANE": lane,
    }

    result = run_gate(["scaffold-lane"], values, tmp_path / "output")

    assert (result.returncode == 0) is passes


@pytest.mark.parametrize(
    ("field", "outcome"),
    [("WORKFLOW_CANCELLED", "false")]
    + [
        (field, outcome)
        for field in SCAFFOLD_JOIN_FIELDS
        for outcome in ("failure", "cancelled", "skipped", "")
    ]
    + [("WORKFLOW_CANCELLED", outcome) for outcome in ("true", "")],
)
def test_scaffold_join_refuses_failed_cancelled_skipped_or_missing_evidence(
    tmp_path: Path, field: str, outcome: str
) -> None:
    output = tmp_path / "output"
    values: dict[str, str | None] = {
        **dict.fromkeys(SCAFFOLD_JOIN_FIELDS, "success"),
        **SCOPE,
        "WORKFLOW_CANCELLED": "false",
        "FULL_SCOPE": "true",
        field: outcome,
    }

    result = run_gate(["scaffold-evidence"], values, output)

    passes = field == "WORKFLOW_CANCELLED" and outcome == "false"
    assert (result.returncode == 0) is passes
    assert recorded(output) == ("verified=true\n" if passes else "")


@pytest.mark.parametrize("gate", list(EDITORIAL_INPUTS))
@pytest.mark.parametrize(
    ("field", "value", "passes"),
    [
        ("SCOPE_RESULT", "success", True),
        ("SCOPE_RESULT", "failure", False),
        ("SCOPE_RESULT", "cancelled", False),
        ("SCOPE_RESULT", "skipped", False),
        ("SCOPE_RESULT", "", False),
        ("SCOPE_VERIFIED", "false", False),
        ("SCOPE_VERIFIED", "", False),
        ("FULL_SCOPE", "", False),
    ],
)
def test_editorial_statuses_require_verified_classification(
    tmp_path: Path, gate: str, field: str, value: str, passes: bool
) -> None:
    output = tmp_path / "output"
    values: dict[str, str | None] = {
        **dict.fromkeys(EDITORIAL_INPUTS[gate], "skipped"),
        **SCOPE,
        "FULL_SCOPE": "false",
        "WORKFLOW_CANCELLED": "false",
        "CANCELLED": "false",
        "SCAFFOLD_RESULT": "success",
        "SCAFFOLD_VERIFIED": "true",
        field: value,
    }

    result = run_gate([gate], values, output)

    assert (result.returncode == 0) is passes
    assert recorded(output) == (
        "verified=true\n" if passes and gate == "scaffold-evidence" else ""
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SCAFFOLD_RESULT", "failure"),
        ("SCAFFOLD_RESULT", "cancelled"),
        ("SCAFFOLD_RESULT", "skipped"),
        ("SCAFFOLD_RESULT", ""),
        ("SCAFFOLD_RESULT", None),
        ("SCAFFOLD_VERIFIED", "false"),
        ("SCAFFOLD_VERIFIED", ""),
        ("SCAFFOLD_VERIFIED", None),
    ],
)
def test_editorial_cel_refuses_missing_or_unsuccessful_shared_proof(
    tmp_path: Path, field: str, value: str | None
) -> None:
    values: dict[str, str | None] = {
        **dict.fromkeys(EDITORIAL_INPUTS["cel-measurements"], "skipped"),
        **SCOPE,
        "FULL_SCOPE": "false",
        "WORKFLOW_CANCELLED": "false",
        "SCAFFOLD_RESULT": "success",
        "SCAFFOLD_VERIFIED": "true",
        field: value,
    }

    result = run_gate(["cel-measurements"], values, tmp_path / "output")

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("field", "outcome"),
    [("WORKFLOW_CANCELLED", "false")]
    + [
        (field, outcome)
        for field in CEL_SUCCESS
        if field != "WORKFLOW_CANCELLED"
        for outcome in ("failure", "cancelled", "skipped", "", "false")
    ]
    + [
        ("WORKFLOW_CANCELLED", "true"),
        ("WORKFLOW_CANCELLED", ""),
        ("SCAFFOLD_RESULT", None),
        ("SCAFFOLD_VERIFIED", None),
    ],
)
def test_required_measurement_gate_fails_closed(
    tmp_path: Path, field: str, outcome: str | None
) -> None:
    values: dict[str, str | None] = {**CEL_SUCCESS, field: outcome}

    result = run_gate(["cel-measurements"], values, tmp_path / "output")

    assert (result.returncode == 0) is (outcome == CEL_SUCCESS[field])


@pytest.mark.parametrize(
    ("full_scope", "browser", "cancelled", "passes"),
    [
        ("true", "success", "false", True),
        ("true", "failure", "false", False),
        ("true", "skipped", "false", False),
        ("true", "cancelled", "false", False),
        ("true", "success", "true", False),
        ("false", "skipped", "false", True),
        ("false", "success", "false", False),
        ("false", "skipped", "true", False),
    ],
)
def test_browser_status_requires_its_checks_or_editorial_skip(
    tmp_path: Path, full_scope: str, browser: str, cancelled: str, passes: bool
) -> None:
    values: dict[str, str | None] = {
        **SCOPE,
        "FULL_SCOPE": full_scope,
        "BROWSER_RESULT": browser,
        "CANCELLED": cancelled,
    }

    result = run_gate(["browser"], values, tmp_path / "output")

    assert (result.returncode == 0) is passes
