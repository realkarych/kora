import os
import shlex
import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.scaffold_evidence import (
    ENVIRONMENTS,
    GENERAL_ARGUMENTS,
    LOCAL_RUNTIME_PATH,
    STORAGE_ARGUMENTS,
)
from tests.support.workflows import (
    action_name,
    command_lines,
    table,
    workflow_jobs,
)

JOIN_FIELDS = (
    "LANES_RESULT",
    "DEPENDENCIES_OUTCOME",
    "DOWNLOAD_OUTCOME",
    "VERIFICATION_OUTCOME",
)


def test_scaffold_protected_names_belong_only_to_complete_joins() -> None:
    jobs = workflow_jobs()
    execution = table(jobs["scaffold-tests"])
    join = table(jobs["scaffold"])

    execution_rows = table(table(execution["strategy"])["matrix"])["include"]

    assert execution["name"] == (
        "Scaffold tests (${{ matrix.os }}, Python ${{ matrix.python }}, "
        "${{ matrix.lane }})"
    )
    assert join["needs"] == ["scaffold-evidence"]
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
            ("local-runtime", (LOCAL_RUNTIME_PATH,)),
        )
    ]
    for job in (execution, join):
        assert job["timeout-minutes"] == 10
        assert table(job["strategy"])["fail-fast"] is False


def test_frontend_consumer_lanes_install_the_pinned_toolchain() -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    entries = [table(value) for value in cast(list[object], job["steps"])]

    actions = [action_name(step["uses"]) for step in entries if "uses" in step]
    node = next(
        step
        for step in entries
        if action_name(step.get("uses", "")) == "actions/setup-node"
    )
    pnpm = next(
        step
        for step in entries
        if action_name(step.get("uses", "")) == "pnpm/action-setup"
    )
    installation = next(
        step for step in entries if step.get("id") == "frontend_dependencies"
    )

    assert table(node["with"])["node-version-file"] == "web/.node-version"
    assert table(pnpm["with"])["package_json_file"] == "web/package.json"
    assert installation["if"] == "${{ matrix.lane != 'local-runtime' }}"
    assert command_lines(installation["run"]) == [
        ("pnpm", "--dir", "web", "install", "--frozen-lockfile")
    ]
    assert entries.index(node) < entries.index(installation)
    assert entries.index(pnpm) < entries.index(installation)
    assert actions.count("actions/setup-node") == 1
    assert actions.count("pnpm/action-setup") == 1


def is_existing_test_path(root: Path, token: str) -> bool:
    path = (root / token).resolve()
    return (
        token.startswith("tests/")
        and path.is_relative_to(root / "tests")
        and path.exists()
    )


def test_scaffold_lane_arguments_only_name_existing_test_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    job = table(workflow_jobs()["scaffold-tests"])
    rows = cast(list[object], table(table(job["strategy"])["matrix"])["include"])

    tokens = [
        token
        for value in rows
        for token in shlex.split(str(table(value)["pytest-arguments"]))
    ]

    assert tokens
    assert [
        token
        for token in tokens
        if not is_existing_test_path(root, token.removeprefix("--ignore="))
    ] == []


def test_scaffold_join_gate_always_runs_on_every_prerequisite_outcome() -> None:
    join = table(workflow_jobs()["scaffold-evidence"])

    gate = table(cast(list[object], join["steps"])[-1])

    assert gate["if"] == "${{ always() }}"
    assert table(gate["env"]) == {
        "SCOPE_RESULT": "${{ needs.change-scope.result }}",
        "SCOPE_VERIFIED": "${{ needs.change-scope.outputs.verified }}",
        "FULL_SCOPE": "${{ needs.change-scope.outputs.full }}",
        "LANES_RESULT": "${{ needs.scaffold-tests.result }}",
        "DEPENDENCIES_OUTCOME": "${{ steps.dependencies.outcome }}",
        "DOWNLOAD_OUTCOME": "${{ steps.evidence_download.outcome }}",
        "VERIFICATION_OUTCOME": "${{ steps.verification.outcome }}",
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }


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
    tmp_path: Path,
) -> None:
    join = table(workflow_jobs()["scaffold-evidence"])
    entries = [table(value) for value in cast(list[object], join["steps"])]
    gate = entries[-1]
    environment = {
        **os.environ,
        **dict.fromkeys(JOIN_FIELDS, "success"),
        "WORKFLOW_CANCELLED": "false",
        "SCOPE_RESULT": "success",
        "SCOPE_VERIFIED": "true",
        "FULL_SCOPE": "true",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        field: outcome,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (
        field == "WORKFLOW_CANCELLED" and outcome == "false"
    )
