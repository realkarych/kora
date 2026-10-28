import os
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from tests.support.workflows import (
    action_name,
    command_lines,
    matrix_rows,
    rendered,
    step_list,
    steps,
    table,
    workflow_document,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = "tests/measurements/test_conformance_lifetime.py"
FIXTURE = "qualification"
INSTALLED = "installed-qualification"
JOIN = "lifetime-evidence"
GATES = ((INSTALLED, "required_measurements"), (JOIN, "required_evidence"))
REVISION = "a" * 40
RECEIPT = "source-revision.txt"
SHARD_FILES = {
    "fixture": ("report.json", "publication.json", "tests.xml", RECEIPT),
    "listing": ("listing.json", "tests.xml", RECEIPT),
    "command": (
        "command.json",
        "command-artifacts/publication.json",
        "tests.xml",
        RECEIPT,
    ),
}
REQUIRED_PATHS = [
    (shard, name) for shard, names in SHARD_FILES.items() for name in names
]
INVENTORY_DAMAGE = [
    ("complete", "", ""),
    ("extra_artifact", "", ""),
    ("foreign_revision", "fixture", RECEIPT),
    *(("missing_shard", shard, "") for shard in SHARD_FILES),
    *(("linked_shard", shard, "") for shard in SHARD_FILES),
    *(
        (damage, shard, name)
        for damage in ("missing", "linked", "empty", "directory")
        for shard, name in REQUIRED_PATHS
    ),
]
FAILED_OUTCOMES = ("failure", "cancelled", "skipped", "")


def lifetime_jobs() -> dict[str, object]:
    return table(workflow_document("conformance-lifetime.yml")["jobs"])


def lifetime_job(job_id: str) -> dict[str, object]:
    return table(lifetime_jobs()[job_id])


def collected_nodes() -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "measurement",
            MODULE,
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    return sorted(
        line
        for line in result.stdout.decode().splitlines()
        if line.startswith(f"{MODULE}::")
    )


def shard_uploads() -> list[str]:
    names: list[str] = []
    for job_id in (FIXTURE, INSTALLED):
        job = lifetime_job(job_id)
        upload = table(steps(job)["lifetime_upload"]["with"])
        names.extend(rendered(upload["name"], row) for row in matrix_rows(job))
    return names


def gate_environment(job_id: str, identity: str) -> dict[str, object]:
    return table(steps(lifetime_job(job_id))[identity]["env"])


def passing_value(name: str) -> str:
    if name == "WORKFLOW_CANCELLED":
        return "false"
    return "true" if name.endswith("_VERIFIED") else "success"


def gate_cases() -> list[tuple[str, str, str, str]]:
    cases: list[tuple[str, str, str, str]] = []
    for job_id, identity in GATES:
        for name in gate_environment(job_id, identity):
            outcomes = (
                ("false", "true", "")
                if name == "WORKFLOW_CANCELLED"
                else (passing_value(name), *FAILED_OUTCOMES)
            )
            cases.extend((job_id, identity, name, outcome) for outcome in outcomes)
    return cases


def source_receipt(checkout: str) -> str:
    return f"Actual checkout: {checkout}\nRequested measurement revision: {REVISION}\n"


def shard_folder(measurements: Path, shard: str) -> Path:
    return measurements / f"conformance-lifetime-{REVISION}-{shard}"


def write_complete_evidence(root: Path) -> Path:
    measurements = root / "measurements"
    for shard, name in REQUIRED_PATHS:
        path = shard_folder(measurements, shard) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_receipt(REVISION) if name == RECEIPT else "{}\n")
    return measurements


def damage_evidence(measurements: Path, damage: str, shard: str, name: str) -> None:
    folder = shard_folder(measurements, shard)
    path = folder / name
    outside = measurements.parent / "outside"
    if damage == "extra_artifact":
        shard_folder(measurements, "unexpected").mkdir()
    elif damage == "foreign_revision":
        path.write_text(source_receipt("b" * 40))
    elif damage == "missing_shard":
        folder.rename(outside)
    elif damage == "linked_shard":
        folder.rename(outside)
        folder.symlink_to(outside, target_is_directory=True)
    elif damage == "missing":
        path.unlink()
    elif damage == "linked":
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif damage == "empty":
        path.write_text("")
    elif damage == "directory":
        path.unlink()
        (path / "content").mkdir(parents=True)


def test_lifetime_shards_select_every_measurement_exactly_once() -> None:
    fixture = steps(lifetime_job(FIXTURE))["lifetime_measurement"]["run"]
    installed = matrix_rows(lifetime_job(INSTALLED))

    selected = sorted(
        [
            command_lines(fixture)[-1][-1],
            *(str(row["pytest-arguments"]) for row in installed),
        ]
    )

    assert selected == collected_nodes()


def test_installed_lifetime_shards_keep_their_own_budget_and_evidence() -> None:
    job = lifetime_job(INSTALLED)
    selected = steps(job)
    fixture = steps(lifetime_job(FIXTURE))
    upload = table(selected["lifetime_upload"]["with"])
    gate = selected["required_measurements"]

    measurement = command_lines(selected["lifetime_measurement"]["run"])

    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 10
    assert "needs" not in job
    assert "continue-on-error" not in job
    assert table(job["strategy"])["fail-fast"] is False
    assert [row["run"] for row in matrix_rows(job)] == ["listing", "command"]
    assert all(set(row) == {"run", "pytest-arguments"} for row in matrix_rows(job))
    assert selected["dependencies"]["run"] == fixture["dependencies"]["run"]
    assert selected["source_revision"] == fixture["source_revision"]
    assert (
        selected["lifetime_measurement"]["env"]
        == fixture["lifetime_measurement"]["env"]
    )
    assert measurement[-1] == (
        "KORA_MEASUREMENT_OUTPUT="
        "$PWD/measurements/conformance-lifetime-${{matrix.run}}/report.json",
        "exec",
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "-q",
        "-p",
        "no:logging",
        "-m",
        "measurement",
        "${{matrix.pytest-arguments}}",
        "--junitxml=measurements/conformance-lifetime-${{matrix.run}}/tests.xml",
    )
    assert action_name(selected["lifetime_upload"]["uses"]) == (
        "actions/upload-artifact"
    )
    assert selected["lifetime_upload"]["if"] == (
        "${{ always() && steps.lifetime_measurement.outcome != 'skipped' }}"
    )
    assert upload == {
        "name": "conformance-lifetime-${{ github.sha }}-${{ matrix.run }}",
        "path": "measurements/conformance-lifetime-${{ matrix.run }}",
        "include-hidden-files": True,
        "if-no-files-found": "error",
    }
    assert all("continue-on-error" not in step for step in step_list(job))
    assert step_list(job)[-1] == gate
    assert gate["if"] == "${{ always() }}"
    assert gate_environment(INSTALLED, "required_measurements") == {
        **{
            f"{identity.upper()}_OUTCOME": f"${{{{ steps.{identity}.outcome }}}}"
            for identity in selected
            if identity not in {"required_measurements", "workflow_cancellation"}
        },
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }


def test_lifetime_join_requires_every_shard_and_its_evidence() -> None:
    join = lifetime_job(JOIN)
    selected = steps(join)
    download = selected["download"]
    gate = selected["required_evidence"]

    pattern = str(table(download["with"])["pattern"])

    assert join["name"] == "Required lifetime evidence"
    assert join["needs"] == [FIXTURE, INSTALLED]
    assert join["if"] == "${{ always() }}"
    assert join["timeout-minutes"] == 10
    assert "continue-on-error" not in join
    assert table(join["env"]) == {"KORA_MEASUREMENT_REVISION": "${{ github.sha }}"}
    assert action_name(download["uses"]) == "actions/download-artifact"
    assert download["if"] == "${{ always() && !cancelled() }}"
    assert table(download["with"]) == {
        "pattern": "conformance-lifetime-${{ github.sha }}-*",
        "path": "measurements",
        "merge-multiple": False,
    }
    assert len(shard_uploads()) == len(SHARD_FILES)
    assert all(fnmatchcase(name, pattern) for name in shard_uploads())
    assert selected["inventory"]["if"] == (
        "${{ always() && !cancelled() && steps.download.outcome == 'success' }}"
    )
    assert selected["workflow_cancellation"]["if"] == "${{ cancelled() }}"
    assert selected["workflow_cancellation"]["run"] == "exit 0"
    assert step_list(join)[-1] == gate
    assert gate["if"] == "${{ always() }}"
    assert gate_environment(JOIN, "required_evidence") == {
        "FIXTURE_RESULT": "${{ needs.qualification.result }}",
        "FIXTURE_VERIFIED": "${{ needs.qualification.outputs.verified }}",
        "INSTALLED_RESULT": "${{ needs.installed-qualification.result }}",
        "DOWNLOAD_OUTCOME": "${{ steps.download.outcome }}",
        "INVENTORY_OUTCOME": "${{ steps.inventory.outcome }}",
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }


@pytest.mark.parametrize(("damage", "shard", "name"), INVENTORY_DAMAGE)
def test_lifetime_inventory_accepts_only_every_shard_with_its_receipt(
    tmp_path: Path, damage: str, shard: str, name: str
) -> None:
    inventory = steps(lifetime_job(JOIN))["inventory"]["run"]
    measurements = write_complete_evidence(tmp_path)
    damage_evidence(measurements, damage, shard, name)

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(inventory)],
        cwd=tmp_path,
        env={**os.environ, "KORA_MEASUREMENT_REVISION": REVISION},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (damage == "complete")


@pytest.mark.parametrize(("job_id", "identity", "field", "outcome"), gate_cases())
def test_lifetime_gates_pass_only_after_complete_success(
    job_id: str, identity: str, field: str, outcome: str
) -> None:
    gate = steps(lifetime_job(job_id))[identity]
    environment = {
        **os.environ,
        **{name: passing_value(name) for name in gate_environment(job_id, identity)},
        field: outcome,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (outcome == passing_value(field))
