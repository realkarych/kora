import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.measurements.storage_qualification_evidence import qualification_nodes
from tests.scaffold_evidence import STORAGE_ARGUMENTS, collection_nodes
from tests.support.workflows import step_list as job_steps
from tests.support.workflows import table, workflow_document

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


def storage_jobs() -> dict[str, object]:
    return table(workflow_document("storage.yml")["jobs"])


def test_storage_qualification_runs_nightly_and_manually() -> None:
    workflow = workflow_document("storage.yml")

    triggers = table(workflow["on"])
    schedule = [table(entry) for entry in cast(list[object], triggers["schedule"])]

    assert set(triggers) == {"workflow_dispatch", "schedule"}
    assert triggers["workflow_dispatch"] is None
    assert schedule
    assert all(isinstance(entry["cron"], str) for entry in schedule)
    assert table(workflow["concurrency"])["cancel-in-progress"] is False


def test_storage_qualification_cases_remain_in_ordinary_collection(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "storage.collection"
    arguments = [sys.executable, "-m", "pytest", "--collect-only", "-q"]

    result = subprocess.run(
        [*arguments, *STORAGE_ARGUMENTS],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    collection.write_bytes(result.stdout)

    assert result.returncode == 0, result.stderr.decode()
    required = qualification_nodes()
    collected = collection_nodes(collection)
    pressure = "tests.plugins.storage.test_pressure_controller."
    required_pressure = frozenset(
        node for node in required if node.startswith(pressure)
    )
    collected_pressure = frozenset(
        node for node in collected if node.startswith(pressure)
    )

    assert required <= collected
    assert required_pressure == collected_pressure


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
    fixtures = next(step for step in selected if step.get("id") == "fixtures")
    measurement = next(
        step for step in selected if "run" in step and "pytest" in str(step["run"])
    )
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
    assert "mktemp -d /tmp/kora-storage-qualification.XXXXXXXX" in str(fixtures["run"])
    for step in (measurement, recovery):
        assert table(step["env"])["KORA_STORAGE_FIXTURES"] == (
            "${{ steps.fixtures.outputs.path }}"
        )
        assert "$KORA_STORAGE_FIXTURES/fixtures" in str(step["run"])
        assert "$RUNNER_TEMP" not in str(step["run"])
    assert selected[-1]["if"] == "${{ always() }}"


def test_storage_required_gate_keeps_failed_dependencies_and_evidence_visible() -> None:
    gate = table(storage_jobs()["storage-evidence"])
    selected = job_steps(gate)
    checks = {str(step["id"]): step for step in selected if "id" in step}
    final = selected[-1]

    assert gate["name"] == "Required storage evidence"
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
    assert "workflow_cancellation" in checks
    assert checks["workflow_cancellation"]["if"] == "${{ cancelled() }}"
    assert checks["workflow_cancellation"]["run"] == "exit 0"
    assert table(final["env"])["WORKFLOW_CANCELLED"] == (
        "${{ steps.workflow_cancellation.outcome != 'skipped' }}"
    )


@pytest.mark.parametrize(
    ("field", "outcome"),
    [
        (field, outcome)
        for field in SUCCESS
        if field != "WORKFLOW_CANCELLED"
        for outcome in ("failure", "cancelled", "skipped", "")
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
