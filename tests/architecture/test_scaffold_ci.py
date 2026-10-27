import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.architecture.test_measurement_job_routing import table, workflow_jobs
from tests.scaffold_evidence import (
    ENVIRONMENTS,
    GENERAL_ARGUMENTS,
    STORAGE_ARGUMENTS,
    collection_nodes,
)

JOIN_FIELDS = (
    "LANES_RESULT",
    "DEPENDENCIES_OUTCOME",
    "DOWNLOAD_OUTCOME",
    "VERIFICATION_OUTCOME",
)
FRONTEND_TOOLCHAIN: list[dict[str, object]] = [
    {
        "uses": "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
        "with": {
            "node-version-file": "web/.node-version",
            "package-manager-cache": False,
        },
    },
    {
        "uses": "pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413",
        "with": {
            "package_json_file": "web/package.json",
            "run_install": False,
            "cache": True,
            "cache_dependency_path": "web/pnpm-lock.yaml",
        },
    },
    {
        "name": "Install locked frontend dependencies",
        "id": "frontend_dependencies",
        "run": "pnpm --dir web install --frozen-lockfile",
    },
]


def test_scaffold_protected_names_belong_only_to_complete_joins() -> None:
    jobs = workflow_jobs()
    execution = table(jobs["scaffold-tests"])
    join = table(jobs["scaffold"])

    execution_rows = table(table(execution["strategy"])["matrix"])["include"]

    assert execution["name"] == (
        "Scaffold tests (${{ matrix.os }}, Python ${{ matrix.python }}, "
        "${{ matrix.lane }})"
    )
    assert join["name"] == "Scaffold (${{ matrix.os }}, Python ${{ matrix.python }})"
    assert join["needs"] == ["scaffold-tests"]
    assert join["if"] == "${{ always() }}"
    assert table(table(join["strategy"])["matrix"])["include"] == [
        {"os": operating_system, "python": python}
        for operating_system, python, _ in ENVIRONMENTS
    ]
    assert execution_rows == [
        {
            "os": operating_system,
            "python": python,
            "databases": databases,
            "lane": lane,
            "pytest-arguments": " ".join(arguments),
        }
        for operating_system, python, databases in ENVIRONMENTS
        for lane, arguments in (
            ("conformance", GENERAL_ARGUMENTS),
            ("remainder", GENERAL_ARGUMENTS),
            ("storage", STORAGE_ARGUMENTS),
        )
    ]
    for job in (execution, join):
        assert job["timeout-minutes"] == 10
        assert table(job["strategy"])["fail-fast"] is False
    entries = [table(value) for value in cast(list[object], execution["steps"])]
    for name, condition in (
        ("Check Python and frontend sources", "${{ matrix.lane == 'conformance' }}"),
        ("Build isolated distributions offline", "${{ matrix.lane == 'conformance' }}"),
    ):
        selected = next(step for step in entries if step.get("name") == name)
        assert selected["if"] == condition


def test_every_scaffold_lane_installs_the_pinned_frontend_toolchain() -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    entries = [table(value) for value in cast(list[object], job["steps"])]

    toolchain = [
        step
        for step in entries
        if str(step.get("uses", "")).startswith(
            ("actions/setup-node@", "pnpm/action-setup@")
        )
        or step.get("id") == "frontend_dependencies"
    ]

    assert toolchain == FRONTEND_TOOLCHAIN


def test_scaffold_workflow_arguments_partition_actual_default_collection(
    tmp_path: Path,
) -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    rows = cast(list[object], table(table(job["strategy"])["matrix"])["include"])
    selections = {
        str(row["lane"]): shlex.split(str(row["pytest-arguments"]))
        for value in rows
        if (row := table(value))["os"] == "macos-15"
    }
    collected: dict[str, frozenset[str]] = {}

    for lane, arguments in {"complete": [], **selections}.items():
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0
        path = tmp_path / f"{lane}.txt"
        path.write_bytes(result.stdout)
        collected[lane] = collection_nodes(path)

    general, storage = collected["conformance"], collected["storage"]
    assert collected["remainder"] == general
    assert len(general & storage) == 0
    assert len((general | storage) ^ collected["complete"]) == 0


@pytest.mark.parametrize(
    ("field", "outcome"),
    [("WORKFLOW_CANCELLED", "false")]
    + [
        (field, outcome)
        for field in JOIN_FIELDS
        for outcome in ("failure", "cancelled", "skipped", "")
    ]
    + [("WORKFLOW_CANCELLED", outcome) for outcome in ("true", "")],
)
def test_scaffold_join_refuses_failed_cancelled_skipped_or_missing_evidence(
    field: str,
    outcome: str,
) -> None:
    join = table(workflow_jobs()["scaffold"])
    entries = [table(value) for value in cast(list[object], join["steps"])]
    gate = entries[-1]
    environment = {
        **os.environ,
        **dict.fromkeys(JOIN_FIELDS, "success"),
        "WORKFLOW_CANCELLED": "false",
        field: outcome,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert gate["if"] == "${{ always() }}"
    assert (result.returncode == 0) is (
        field == "WORKFLOW_CANCELLED" and outcome == "false"
    )
    assert table(gate["env"]) == {
        "LANES_RESULT": "${{ needs.scaffold-tests.result }}",
        "DEPENDENCIES_OUTCOME": "${{ steps.dependencies.outcome }}",
        "DOWNLOAD_OUTCOME": "${{ steps.evidence_download.outcome }}",
        "VERIFICATION_OUTCOME": "${{ steps.verification.outcome }}",
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }
