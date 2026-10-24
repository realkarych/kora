import os
import subprocess
from pathlib import Path
from typing import Protocol, cast

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
SUCCESS: dict[str, str] = {
    "LINUX_RESULT": "success",
    "MACOS_RESULT": "success",
    "QUALIFICATION_RESULT": "success",
    "DOWNLOAD_RESULT": "success",
    "UNION_RESULT": "success",
    "UPLOAD_RESULT": "success",
    "WORKFLOW_CANCELLED": "false",
}


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


def table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def storage_jobs() -> dict[str, object]:
    document = cast(YamlLoader, YAML(typ="safe")).load(
        ROOT / ".github/workflows/storage.yml"
    )
    return table(table(document)["jobs"])


def job_steps(job: dict[str, object]) -> list[dict[str, object]]:
    return [table(entry) for entry in cast(list[object], job["steps"])]


def test_storage_ci_requires_exact_platform_producers_and_always_qualification() -> (
    None
):
    jobs = storage_jobs()

    for name, platform, runner in (
        ("storage-lifetime-linux", "linux", "ubuntu-24.04"),
        ("storage-lifetime-macos", "darwin", "macos-15"),
    ):
        job = table(jobs[name])
        strategy = table(job["strategy"])
        selected = job_steps(job)
        measurement = next(
            step for step in selected if "run" in step and "pytest" in str(step["run"])
        )
        upload = selected[-1]

        assert job["runs-on"] == runner
        assert job["timeout-minutes"] == 10
        assert strategy["fail-fast"] is False
        assert table(strategy["matrix"])["repeat"] == [0, 1, 2]
        assert table(selected[0]["with"]) == {
            "persist-credentials": False,
            "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
            "fetch-depth": 0,
        }
        assert "test_artifact_storage_lifetime[${{ matrix.repeat }}]" in str(
            measurement["run"]
        )
        assert "--junitxml=measurements/" + platform in str(measurement["run"])
        assert upload["if"] == "${{ always() }}"
        assert table(upload["with"])["if-no-files-found"] == "error"
        assert (
            table(upload["with"])["name"]
            == "storage-${{ env.KORA_MEASUREMENT_REVISION }}-"
            + platform
            + "-${{ matrix.repeat }}"
        )
    qualification = table(jobs["storage-qualification"])
    selected = job_steps(qualification)
    recovery = next(
        step
        for step in selected
        if step.get("name") == "Recover fixture ownership and preserve reports"
    )

    assert table(table(qualification["strategy"])["matrix"])["include"] == [
        {"os": "ubuntu-24.04", "platform": "linux"},
        {"os": "macos-15", "platform": "darwin"},
    ]
    assert recovery["if"] == "${{ always() }}"
    assert "tests.measurements.storage_pressure_evidence" in str(recovery["run"])
    assert selected[-1]["if"] == "${{ always() }}"


def test_storage_required_gate_keeps_failed_dependencies_and_evidence_visible() -> None:
    gate = table(storage_jobs()["storage-evidence"])
    selected = job_steps(gate)
    checks = {str(step["id"]): step for step in selected if "id" in step}
    final = selected[-1]

    assert gate["needs"] == [
        "storage-lifetime-linux",
        "storage-lifetime-macos",
        "storage-qualification",
    ]
    assert gate["if"] == "${{ always() }}"
    assert final["if"] == "${{ always() }}"
    assert set(table(final["env"])) == set(SUCCESS)
    assert "!cancelled()" in str(checks["download"]["if"])
    assert table(checks["download"]["with"])["merge-multiple"] is False
    assert "steps.download.outcome == 'success'" in str(checks["union"]["if"])
    assert checks["upload"]["if"] == "${{ always() }}"
    assert table(checks["upload"]["with"])["if-no-files-found"] == "error"


@pytest.mark.parametrize(
    ("field", "outcome"),
    [
        (field, outcome)
        for field in SUCCESS
        if field != "WORKFLOW_CANCELLED"
        for outcome in ("success", "failure", "cancelled", "skipped", "")
    ]
    + [
        ("WORKFLOW_CANCELLED", "false"),
        ("WORKFLOW_CANCELLED", "true"),
        ("WORKFLOW_CANCELLED", ""),
    ],
)
def test_storage_required_shell_gate_preserves_failure(
    field: str, outcome: str
) -> None:
    final = job_steps(table(storage_jobs()["storage-evidence"]))[-1]
    environment = {**os.environ, **SUCCESS, field: outcome}

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(final["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (outcome == SUCCESS[field])
