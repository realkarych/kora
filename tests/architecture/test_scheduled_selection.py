import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.architecture.test_ordinary_ci import selected_files
from tests.scaffold_evidence import (
    ENVIRONMENTS,
    SCHEDULED_LANES,
    SCHEDULED_LIFETIME_MODULES,
    SCHEDULED_MACOS_STORAGE_MODULES,
    STORAGE_ARGUMENTS,
    lane_arguments,
)
from tests.support.workflows import job_of, repository_workflows, table

ROOT = Path(__file__).resolve().parents[2]
LANE_SCRIPT = ROOT / "scripts/ci/scaffold_lane.sh"
SCHEDULED_SCRIPT = ROOT / "scripts/ci/scheduled_lanes.sh"
DAILY = "daily-lanes.yml"
TEST_JOBS = ("lifetime-tests", "storage-pressure-tests")
MEASUREMENT_JOBS = (
    "ir-measurements",
    "resource-measurements",
    "provider-protocol-measurements",
    "pricing-measurements",
    "git-publication-measurements",
)
PULL_REQUEST_JOBS = ("cel-producer", "cel-measurements")
Environment = tuple[str, str, str]


def shell_array(
    script: Path, command: str, array: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    program = (
        f'source "$1"\n{command}\n'
        f'for item in "${{{array}[@]}}"; do printf "%s\\0" "$item"; done\n'
    )
    return subprocess.run(
        ["/bin/bash", "-c", program, "bash", str(script)],
        env={**os.environ, **environment},
        capture_output=True,
        check=False,
        timeout=5,
    )


def printed(result: subprocess.CompletedProcess[bytes]) -> tuple[str, ...]:
    assert result.returncode == 0, result.stderr
    return tuple(result.stdout.decode().split("\0")[:-1])


def select_lane(operating_system: str, lane: str) -> subprocess.CompletedProcess[bytes]:
    return shell_array(
        SCHEDULED_SCRIPT,
        'select_scheduled_lane "$KORA_SCHEDULED_OS" "$KORA_SCHEDULED_LANE"',
        "KORA_SCHEDULED_SELECTION",
        {"KORA_SCHEDULED_OS": operating_system, "KORA_SCHEDULED_LANE": lane},
    )


def scheduled_selection(operating_system: str, lane: str) -> tuple[str, ...]:
    return printed(select_lane(operating_system, lane))


def complete_arguments(operating_system: str) -> tuple[str, ...]:
    return printed(
        shell_array(
            LANE_SCRIPT,
            "complete_arguments",
            "KORA_COMPLETE_ARGUMENTS",
            {"KORA_SCAFFOLD_OS": operating_system},
        )
    )


def storage_selection(operating_system: str) -> tuple[str, ...]:
    return printed(
        shell_array(
            LANE_SCRIPT,
            "lane_selection",
            "KORA_SCAFFOLD_SELECTION",
            {"KORA_SCAFFOLD_OS": operating_system, "KORA_SCAFFOLD_LANE": "storage"},
        )
    )


def without_markers(arguments: tuple[str, ...]) -> list[str]:
    assert arguments[:2] == ("-m", "not measurement")
    return list(arguments[2:])


def daily_rows() -> dict[str, list[Environment]]:
    workflows = repository_workflows()
    rows: dict[str, list[Environment]] = {}
    for identity in TEST_JOBS:
        job = job_of(workflows, DAILY, identity)
        environment = table(job["env"])
        matrix = table(table(job["strategy"])["matrix"])
        included = [table(row) for row in cast(list[object], matrix["include"])]
        rows.setdefault(str(environment["KORA_SCHEDULED_LANE"]), []).extend(
            (str(row["os"]), str(row["python"]), str(row["databases"]))
            for row in included
        )
    return rows


def daily_modules() -> dict[Environment, frozenset[str]]:
    modules: dict[Environment, frozenset[str]] = {}
    for lane, environments in daily_rows().items():
        for environment in environments:
            selection = frozenset(scheduled_selection(environment[0], lane))
            modules[environment] = modules.get(environment, frozenset()) | selection
    return modules


@pytest.mark.parametrize(("operating_system", "lane"), list(SCHEDULED_LANES))
def test_shell_and_evidence_join_name_the_same_scheduled_modules(
    operating_system: str, lane: str
) -> None:
    expected = SCHEDULED_LANES[(operating_system, lane)]

    selection = scheduled_selection(operating_system, lane)

    assert selection == expected
    assert all((ROOT / module).is_file() for module in selection)


@pytest.mark.parametrize(
    ("operating_system", "lane"),
    [("ubuntu-24.04", "storage"), ("macos-26", "lifetime"), ("ubuntu-24.04", "")],
)
def test_shell_refuses_an_unscheduled_lane(operating_system: str, lane: str) -> None:
    result = select_lane(operating_system, lane)

    assert result.returncode != 0
    assert result.stdout == b""


@pytest.mark.parametrize("identity", TEST_JOBS)
def test_daily_test_jobs_run_in_their_matrix_environment(identity: str) -> None:
    job = job_of(repository_workflows(), DAILY, identity)

    environment = table(job["env"])

    assert job["runs-on"] == "${{ matrix.os }}"
    assert environment["KORA_SCHEDULED_OS"] == "${{ matrix.os }}"
    assert environment["KORA_TEST_DATABASES"] == "${{ matrix.databases }}"


def test_daily_workflow_runs_each_scheduled_lane_in_its_environments() -> None:
    expected = {
        "lifetime": list(ENVIRONMENTS),
        "storage": [("macos-15", "3.11.17", "sqlite")],
    }

    rows = daily_rows()

    assert rows == expected


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pull_request_and_daily_selections_partition_every_test_module(
    environment: Environment,
) -> None:
    every_module = selected_files([])
    scheduled = daily_modules()[environment]

    pull_request = selected_files(without_markers(complete_arguments(environment[0])))

    assert scheduled
    assert pull_request.isdisjoint(scheduled)
    assert pull_request | scheduled == every_module


@pytest.mark.parametrize(
    ("operating_system", "keeps_pressure"),
    [("ubuntu-24.04", True), ("macos-15", False)],
)
def test_storage_lane_selection_matches_its_receipt_arguments(
    operating_system: str, keeps_pressure: bool
) -> None:
    arguments = lane_arguments(operating_system, "storage")
    pressure = frozenset(SCHEDULED_MACOS_STORAGE_MODULES)

    selection = storage_selection(operating_system)

    assert (*selection, *STORAGE_ARGUMENTS) == arguments
    selected = selected_files(list(arguments))
    assert (pressure <= selected) is keeps_pressure
    assert selected.isdisjoint(pressure) is not keeps_pressure


def test_measurement_jobs_run_only_in_the_daily_workflow() -> None:
    workflows = repository_workflows()
    pull_request = table(workflows["ci.yml"]["jobs"])
    daily = table(workflows[DAILY]["jobs"])

    assert all(job not in pull_request for job in MEASUREMENT_JOBS)
    assert all(job in daily for job in MEASUREMENT_JOBS)
    assert all(job in pull_request for job in PULL_REQUEST_JOBS)
    assert all(job not in daily for job in PULL_REQUEST_JOBS)


def test_pull_requests_that_edit_a_scheduled_input_run_the_daily_workflow() -> None:
    expected = [
        ".github/workflows/daily-lanes.yml",
        "scripts/ci/scheduled_lanes.sh",
        "scripts/ci/scheduled_report.py",
        "tests/scaffold_evidence.py",
        *SCHEDULED_LIFETIME_MODULES,
        *SCHEDULED_MACOS_STORAGE_MODULES,
    ]

    trigger = table(table(repository_workflows()[DAILY]["on"])["pull_request"])

    assert trigger["paths"] == expected
    assert all((ROOT / path).is_file() for path in expected)
