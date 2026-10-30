import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.support.workflows import step_list, table, workflow_jobs


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("EVIDENCE_RESULT", "failure"),
        ("EVIDENCE_RESULT", "cancelled"),
        ("EVIDENCE_RESULT", "skipped"),
        ("EVIDENCE_RESULT", ""),
        ("EVIDENCE_VERIFIED", "false"),
        ("EVIDENCE_VERIFIED", ""),
    ],
)
def test_protected_scaffold_status_requires_actual_shared_proof(
    field: str, value: str
) -> None:
    gate = step_list(table(workflow_jobs()["scaffold"]))[0]
    environment = {
        **os.environ,
        "EVIDENCE_RESULT": "success",
        "EVIDENCE_VERIFIED": "true",
        field: value,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode != 0


@pytest.mark.parametrize("job_id", ["scaffold-evidence", "cel-measurements", "browser"])
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
    tmp_path: Path, job_id: str, field: str, value: str, passes: bool
) -> None:
    job = table(workflow_jobs()[job_id])
    gate = step_list(job)[-1]
    declared = table(gate["env"])
    environment = {
        **os.environ,
        **dict.fromkeys(declared, "skipped"),
        "SCOPE_RESULT": "success",
        "SCOPE_VERIFIED": "true",
        "FULL_SCOPE": "false",
        "WORKFLOW_CANCELLED": "false",
        "CANCELLED": "false",
        "SCAFFOLD_RESULT": "success",
        "SCAFFOLD_VERIFIED": "true",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        field: value,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert (result.returncode == 0) is passes
    if job_id == "scaffold-evidence":
        output = tmp_path / "output"
        if passes:
            assert output.read_text() == "verified=true\n"
        else:
            assert not output.exists()


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
    field: str, value: str | None
) -> None:
    gate = step_list(table(workflow_jobs()["cel-measurements"]))[-1]
    environment = {
        **os.environ,
        **dict.fromkeys(table(gate["env"]), "skipped"),
        "SCOPE_RESULT": "success",
        "SCOPE_VERIFIED": "true",
        "FULL_SCOPE": "false",
        "WORKFLOW_CANCELLED": "false",
        "SCAFFOLD_RESULT": "success",
        "SCAFFOLD_VERIFIED": "true",
    }
    if value is None:
        del environment[field]
    else:
        environment[field] = value

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode != 0


def test_numerical_runtime_diagnostics_cannot_consume_required_lane_deadlines() -> None:
    jobs = workflow_jobs()
    diagnostic = table(jobs["local-runtime-diagnostics"])
    required = table(jobs["scaffold-tests"])

    names = {str(step.get("id")) for step in step_list(required)}

    assert diagnostic["continue-on-error"] is True
    assert diagnostic["timeout-minutes"] == 10
    assert "diagnostics" not in names
    assert "teardown" in names
    assert "local-runtime-diagnostics" not in cast(list[object], required["needs"])
    assert "local-runtime-diagnostics" not in cast(
        list[object], table(jobs["cel-measurements"])["needs"]
    )
