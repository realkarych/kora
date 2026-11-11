import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.measurements.ir_measurement_evidence import expected_evidence, shard_evidence

ROOT = Path(__file__).resolve().parents[2]
SHARD_SCRIPT = ROOT / "scripts/ci/measure_ir_shard.sh"
INVENTORY_SCRIPT = ROOT / "scripts/ci/check_evidence_inventory.sh"
MEASUREMENT = "tests/measurements/test_ir_models_cost.py"
RUN = f"{MEASUREMENT}::test_ir_configuration_repeated_lifetime"


def shard_selection(run: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"\nselect_ir_shard "$2"\nprintf "%s\\0" "${selection[@]}"',
            "bash",
            str(SHARD_SCRIPT),
            run,
        ],
        capture_output=True,
        check=False,
        timeout=5,
    )


def shard_arguments(run: int) -> list[str]:
    result = shard_selection(str(run))
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().split("\0")[:-1]


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
    arguments = [shard_arguments(run) for run in range(len(shards))]
    workers = (f"{RUN}[1]", f"{RUN}[2]")

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
    assert collected[1][1] == (
        *workers,
        *(
            node
            for node in default_nodes
            if node in shards[1].nodes and node not in workers
        ),
    )
    assert collected[2][1] == tuple(
        node for node in default_nodes if node in shards[2].nodes
    )
    assert set().union(*(nodes for _, nodes in collected)) == expected.nodes
    assert sum(len(nodes) for _, nodes in collected) == len(expected.nodes)


@pytest.mark.parametrize("run", ["6", "-1", "", "0 1"])
def test_ir_shard_script_refuses_an_unknown_shard(run: str) -> None:
    result = shard_selection(run)

    assert result.returncode != 0
    assert result.stdout == b""


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


@pytest.mark.parametrize(
    ("mutation", "arm"),
    [
        ("valid", "lifetime"),
        *(
            (mutation, arm)
            for mutation in (
                "missing",
                "empty",
                "extra",
                "revision",
                "symlink",
                "directory",
            )
            for arm in ("lifetime", "cost")
        ),
    ],
)
def test_actual_cel_inventory_rejects_incomplete_or_mixed_evidence(
    tmp_path: Path, arm: str, mutation: str
) -> None:
    revision = "a" * 40
    for directory, filename in (
        ("lifetime", "cel-evaluator-lifetime.json"),
        ("cost", "cel-cost-smoke.json"),
    ):
        target = tmp_path / "measurements" / f"cel-{directory}"
        target.mkdir(parents=True)
        (target / filename).write_text("{}\n")
        (target / "cel-checkout-revision.txt").write_text(revision + "\n")
    directory = tmp_path / "measurements" / f"cel-{arm}"
    report = directory / (
        "cel-evaluator-lifetime.json" if arm == "lifetime" else "cel-cost-smoke.json"
    )
    if mutation == "missing":
        report.unlink()
    elif mutation == "empty":
        report.write_text("")
    elif mutation == "extra":
        (directory / "unexpected.txt").write_text("extra")
    elif mutation == "revision":
        (directory / "cel-checkout-revision.txt").write_text("b" * 40 + "\n")
    elif mutation == "symlink":
        report.unlink()
        report.symlink_to(directory / "cel-checkout-revision.txt")
    elif mutation == "directory":
        report.unlink()
        report.mkdir()

    result = subprocess.run(
        ["/bin/bash", str(INVENTORY_SCRIPT), "cel"],
        cwd=tmp_path,
        env={**os.environ, "KORA_MEASUREMENT_REVISION": revision},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (mutation == "valid")
