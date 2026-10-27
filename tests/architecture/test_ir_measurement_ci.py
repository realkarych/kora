import os
import shlex
import subprocess
import sys
from typing import cast

import pytest

from tests.measurements.ir_measurement_evidence import expected_evidence, shard_evidence
from tests.support.workflows import (
    action_name,
    command_lines,
    step_list,
    steps,
    table,
    workflow_jobs,
)

MEASUREMENT = "tests/measurements/test_ir_models_cost.py"
RUN = f"{MEASUREMENT}::test_ir_configuration_repeated_lifetime"
SCHEMA_RUN = f"{MEASUREMENT}::test_ir_schema_repeated_lifetime"
NEEDED_JOBS = {
    "scaffold": "SCAFFOLD",
    "ir-measurements": "IR_MATRIX",
    "resource-measurements": "RESOURCE",
    "pricing-measurements": "PRICING",
    "cel-producer": "CEL",
    "git-publication-measurements": "GIT_PUBLICATION",
}
SUCCESS = {
    "SCAFFOLD_RESULT": "success",
    "IR_MATRIX_RESULT": "success",
    "RESOURCE_RESULT": "success",
    "RESOURCE_VERIFIED": "true",
    "PRICING_RESULT": "success",
    "PRICING_VERIFIED": "true",
    "CEL_RESULT": "success",
    "CEL_VERIFIED": "true",
    "GIT_PUBLICATION_RESULT": "success",
    "GIT_PUBLICATION_VERIFIED": "true",
    "DEPENDENCIES_OUTCOME": "success",
    "SOURCE_REVISION_OUTCOME": "success",
    "IR_DOWNLOAD_OUTCOME": "success",
    "IR_INVENTORY_OUTCOME": "success",
    "IR_UNION_UPLOAD_OUTCOME": "success",
    "CEL_LIFETIME_DOWNLOAD_OUTCOME": "success",
    "CEL_COST_DOWNLOAD_OUTCOME": "success",
    "CEL_INVENTORY_OUTCOME": "success",
    "WORKFLOW_CANCELLED": "false",
}


def test_ir_ci_uses_exact_literal_nodes_and_required_artifacts() -> None:
    jobs = workflow_jobs()
    job = table(jobs["ir-measurements"])

    strategy = table(job["strategy"])
    includes = cast(list[object], table(strategy["matrix"])["include"])
    selected_steps = steps(job)
    entries = step_list(job)
    upload = selected_steps["ir_upload"]
    upload_with = table(upload["with"])
    measurement = command_lines(selected_steps["measurement"]["run"])

    assert strategy["fail-fast"] is False
    assert [table(row)["run"] for row in includes] == list(range(len(shard_evidence())))
    assert all(set(table(row)) == {"run", "pytest-arguments"} for row in includes)
    assert job["runs-on"] == "ubuntu-24.04"
    assert measurement == [
        (
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
            f"--junitxml={upload_with['path']}ir-models-cost-${{{{matrix.run}}}}.xml",
        )
    ]
    assert upload["if"] == "${{ always() }}"
    assert upload_with["if-no-files-found"] == "error"
    assert upload_with["name"] == (
        "ir-models-cost-measurement-${{ env.KORA_MEASUREMENT_REVISION }}-"
        "${{ matrix.run }}"
    )
    assert action_name(upload["uses"]) == "actions/upload-artifact"
    assert action_name(entries[0]["uses"]) == "actions/checkout"
    assert entries[0]["with"] == {
        "persist-credentials": False,
        "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
        "fetch-depth": 0,
    }
    assert action_name(entries[1]["uses"]) == "astral-sh/setup-uv"
    install = str(entries[2]["run"])
    assert "uv sync --locked --no-build" in install
    assert "uv export --locked --only-group build --no-emit-project" in install
    assert (
        '--build-constraints "$KORA_IR_BUILD_CONSTRAINT_DIR/constraints.txt"' in install
    )
    assert "--require-build-hashes" in install
    assert "--editable ." in install


def test_required_cel_job_and_gate_run_after_all_dependency_outcomes() -> None:
    jobs = workflow_jobs()
    job = table(jobs["cel-measurements"])

    selected_steps = steps(job)
    gate = selected_steps["required_measurements"]
    expected = {
        **{
            f"{prefix}_RESULT": f"${{{{ needs.{needed}.result }}}}"
            for needed, prefix in NEEDED_JOBS.items()
        },
        **{
            f"{prefix}_VERIFIED": f"${{{{ needs.{needed}.outputs.verified }}}}"
            for needed, prefix in NEEDED_JOBS.items()
            if "verified" in table(table(jobs[needed]).get("outputs", {}))
        },
        **{
            f"{identity.upper()}_OUTCOME": f"${{{{ steps.{identity}.outcome }}}}"
            for identity in selected_steps
            if identity not in {"required_measurements", "workflow_cancellation"}
        },
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }

    assert job["name"] == "CEL evaluator lifetime"
    assert job["needs"] == list(NEEDED_JOBS)
    assert job["if"] == "${{ always() }}"
    assert gate["if"] == "${{ always() }}"
    assert table(gate["env"]) == expected
    assert set(expected) == set(SUCCESS)
    assert selected_steps["workflow_cancellation"]["if"] == "${{ cancelled() }}"
    assert selected_steps["workflow_cancellation"]["run"] == "exit 0"
    assert action_name(selected_steps["ir_download"]["uses"]) == (
        "actions/download-artifact"
    )
    assert table(selected_steps["ir_download"]["with"])["merge-multiple"] is False


@pytest.mark.parametrize(
    ("field", "outcome"),
    [("WORKFLOW_CANCELLED", "false")]
    + [
        (field, outcome)
        for field in SUCCESS
        if field != "WORKFLOW_CANCELLED"
        for outcome in ("failure", "cancelled", "skipped", "", "false")
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


def collected_nodes(arguments: list[str]) -> tuple[int, tuple[str, ...]]:
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
    return result.returncode, tuple(
        line
        for line in result.stdout.splitlines()
        if line.startswith(MEASUREMENT + "::")
    )


def test_ir_literal_selections_preserve_the_default_node_union() -> None:
    expected = expected_evidence()
    shards = shard_evidence()
    job = table(workflow_jobs()["ir-measurements"])
    includes = cast(list[object], table(table(job["strategy"])["matrix"])["include"])
    arguments = [
        shlex.split(str(table(entry)["pytest-arguments"])) for entry in includes
    ]

    collected = [collected_nodes(selection) for selection in arguments]
    default_code, default_nodes = collected_nodes([MEASUREMENT])

    assert default_code == 0
    assert frozenset(default_nodes) == expected.nodes
    assert len(default_nodes) == len(expected.nodes)
    assert all(code == 0 for code, _ in collected)
    assert [frozenset(nodes) for _, nodes in collected] == [
        shard.nodes for shard in shards
    ]
    assert collected[0][1] == tuple(
        node for node in default_nodes if node in shards[0].nodes
    )
    for run in (1, 2):
        worker = f"{RUN}[{run}]"
        assert collected[run][1] == (
            worker,
            *(
                node
                for node in default_nodes
                if node in shards[run].nodes and node != worker
            ),
        )
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
    assert nodes == ()
