import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest
from ruamel.yaml import YAML

from tests.measurements.ir_measurement_evidence import expected_evidence

ROOT = Path(__file__).resolve().parents[2]
MEASUREMENT = "tests/measurements/test_ir_models_cost.py"
RUN = f"{MEASUREMENT}::test_ir_configuration_repeated_lifetime"
SUCCESS = {
    "IR_MATRIX_RESULT": "success",
    "IR_DOWNLOAD_OUTCOME": "success",
    "IR_INVENTORY_OUTCOME": "success",
    "IR_UNION_UPLOAD_OUTCOME": "success",
    "CEL_LIFETIME_OUTCOME": "success",
    "CEL_COST_OUTCOME": "success",
    "CEL_ARTIFACTS_OUTCOME": "success",
    "CEL_LIFETIME_UPLOAD_OUTCOME": "success",
    "CEL_COST_UPLOAD_OUTCOME": "success",
    "WORKFLOW_CANCELLED": "false",
}


def table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


def workflow_jobs() -> dict[str, object]:
    document = cast(YamlLoader, YAML(typ="safe")).load(
        ROOT / ".github" / "workflows" / "ci.yml"
    )
    return table(table(document)["jobs"])


def steps(job: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = cast(list[object], job["steps"])
    return {
        str(entry["id"]): entry for value in entries if "id" in (entry := table(value))
    }


def test_ir_ci_uses_exact_literal_nodes_and_required_artifacts() -> None:
    jobs = workflow_jobs()
    job = table(jobs["ir-measurements"])

    strategy = table(job["strategy"])
    includes = table(strategy["matrix"])["include"]
    selected_steps = steps(job)

    assert strategy["fail-fast"] is False
    assert includes == [
        {
            "run": 0,
            "pytest-arguments": (
                f"{MEASUREMENT} '--deselect={RUN}[1]' '--deselect={RUN}[2]'"
            ),
        },
        {"run": 1, "pytest-arguments": f"'{RUN}[1]'"},
        {"run": 2, "pytest-arguments": f"'{RUN}[2]'"},
    ]
    assert job["runs-on"] == "ubuntu-24.04"
    assert selected_steps["measurement"]["run"] == (
        "uv run --no-sync pytest -q -p no:logging -m measurement "
        "${{ matrix.pytest-arguments }} "
        "--junitxml=measurements/ir-models-cost-${{ matrix.run }}.xml\n"
    )
    upload = selected_steps["ir_upload"]
    assert upload["if"] == "${{ always() }}"
    assert table(upload["with"])["if-no-files-found"] == "error"
    assert table(upload["with"])["name"] == (
        "ir-models-cost-measurement-${{ env.KORA_MEASUREMENT_REVISION }}-"
        "${{ matrix.run }}"
    )
    entries = [table(entry) for entry in cast(list[object], job["steps"])]
    assert entries[0]["uses"] == (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    )
    assert entries[0]["with"] == {
        "persist-credentials": False,
        "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
        "fetch-depth": 0,
    }
    assert entries[1]["uses"] == (
        "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
    )
    assert table(entries[1]["with"])["version"] == "0.13.1"
    assert table(entries[1]["with"])["python-version"] == "3.11.17"
    install = str(entries[2]["run"])
    assert "uv sync --locked --no-build" in install
    assert "uv export --locked --only-group build --no-emit-project" in install
    assert (
        '--build-constraints "$KORA_IR_BUILD_CONSTRAINT_DIR/constraints.txt"' in install
    )
    assert "--require-build-hashes" in install
    assert "--editable ." in install


def test_required_cel_job_and_gate_run_after_all_dependency_outcomes() -> None:
    job = table(workflow_jobs()["cel-measurements"])

    selected_steps = steps(job)
    gate = selected_steps["required_measurements"]

    assert job["name"] == "CEL evaluator lifetime"
    assert job["needs"] == ["ir-measurements"]
    assert job["if"] == "${{ always() }}"
    assert gate["if"] == "${{ always() }}"
    assert table(gate["env"]) == {
        "IR_MATRIX_RESULT": "${{ needs.ir-measurements.result }}",
        "IR_DOWNLOAD_OUTCOME": "${{ steps.ir_download.outcome }}",
        "IR_INVENTORY_OUTCOME": "${{ steps.ir_inventory.outcome }}",
        "IR_UNION_UPLOAD_OUTCOME": "${{ steps.ir_union_upload.outcome }}",
        "CEL_LIFETIME_OUTCOME": "${{ steps.measurement.outcome }}",
        "CEL_COST_OUTCOME": "${{ steps.cost_regression.outcome }}",
        "CEL_ARTIFACTS_OUTCOME": "${{ steps.cel_artifacts.outcome }}",
        "CEL_LIFETIME_UPLOAD_OUTCOME": "${{ steps.lifetime_upload.outcome }}",
        "CEL_COST_UPLOAD_OUTCOME": "${{ steps.cost_upload.outcome }}",
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }
    assert selected_steps["workflow_cancellation"]["if"] == "${{ cancelled() }}"
    assert selected_steps["workflow_cancellation"]["run"] == "exit 0"
    assert selected_steps["ir_download"]["uses"] == (
        "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0"
    )
    assert table(selected_steps["ir_download"]["with"])["merge-multiple"] is False


@pytest.mark.parametrize(
    ("field", "outcome"),
    [
        (field, outcome)
        for field in SUCCESS
        if field != "WORKFLOW_CANCELLED"
        for outcome in ("success", "failure", "cancelled", "skipped", "")
    ]
    + [("WORKFLOW_CANCELLED", "true"), ("WORKFLOW_CANCELLED", "")],
)
def test_required_measurement_shell_gate_fails_closed(field: str, outcome: str) -> None:
    gate = steps(table(workflow_jobs()["cel-measurements"]))["required_measurements"]
    environment = {**os.environ, **SUCCESS, field: outcome}

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert (result.returncode == 0) is (outcome == SUCCESS[field])


def collected_nodes(arguments: list[str]) -> tuple[int, frozenset[str]]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "measurement",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode, frozenset(
        line
        for line in result.stdout.splitlines()
        if line.startswith(MEASUREMENT + "::")
    )


def test_ir_literal_selections_preserve_the_default_node_union() -> None:
    expected = expected_evidence()
    arguments = [
        [MEASUREMENT, f"--deselect={RUN}[1]", f"--deselect={RUN}[2]"],
        [f"{RUN}[1]"],
        [f"{RUN}[2]"],
    ]

    collected = [collected_nodes(selection) for selection in arguments]
    default_code, default_nodes = collected_nodes([MEASUREMENT])

    assert default_code == 0
    assert default_nodes == expected.nodes
    assert all(code == 0 for code, _ in collected)
    assert [len(nodes) for _, nodes in collected] == [350, 1, 1]
    assert set().union(*(nodes for _, nodes in collected)) == expected.nodes
    assert sum(len(nodes) for _, nodes in collected) == len(expected.nodes)


@pytest.mark.parametrize(
    ("selection", "expected_code"),
    [([f"{RUN}[99]"], 4), ([MEASUREMENT, "-k", "no_such_ir_measurement"], 5)],
)
def test_ir_unknown_or_empty_selection_fails(
    selection: list[str], expected_code: int
) -> None:
    code, nodes = collected_nodes(selection)

    assert code == expected_code
    assert nodes == frozenset()
