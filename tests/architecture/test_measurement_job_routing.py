import os
import re
import stat
import subprocess
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path
from typing import cast

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
    workflow_jobs,
)


@dataclass(frozen=True)
class ProducerEvidence:
    measurements: tuple[str, ...]
    uploads: tuple[str, ...]


PRODUCER_EVIDENCE = {
    "resource-measurements": ProducerEvidence(
        (
            "measurement",
            "database_measurement",
            "full_disk",
        ),
        (
            "logging_upload",
            "database_upload",
            "full_disk_upload",
        ),
    ),
    "provider-protocol-measurements": ProducerEvidence(
        ("provider_loading_measurement", "protocol_models_measurement"),
        (
            "provider_upload",
            "protocol_upload",
        ),
    ),
    "qualification": ProducerEvidence(("lifetime_measurement",), ("lifetime_upload",)),
    "pricing-measurements": ProducerEvidence(
        ("pricing_measurement",), ("pricing_upload",)
    ),
    "git-publication-measurements": ProducerEvidence(
        ("git_publication_measurement",), ("git_publication_upload",)
    ),
    "cel-producer": ProducerEvidence(
        ("measurement", "cost_regression"), ("cost_upload", "lifetime_upload")
    ),
}
PRODUCER_JOBS = tuple(PRODUCER_EVIDENCE)
MATRIX_PRODUCER = "ir-measurements"
INSTALLED_LIFETIME = "installed-qualification"
GATED_JOBS = (*PRODUCER_JOBS, INSTALLED_LIFETIME)
SHARDED_MEASUREMENT = "tests/measurements/test_conformance_lifetime.py"
LIFETIME_JOBS = frozenset(table(workflow_document("conformance-lifetime.yml")["jobs"]))
GATE = "required_measurements"
CANCELLATION = "workflow_cancellation"
ROOT = Path(__file__).resolve().parents[2]
COMMAND_ROUTED_MEASUREMENT = "tests/measurements/test_pricing_database_lifetime.py"
STORAGE_WORKFLOW_MEASUREMENT = "tests/measurements/test_artifact_storage_lifetime.py"
LANE_DIAGNOSTIC_MEASUREMENT = "tests/measurements/test_local_runtime_cost.py"
MEASUREMENT_MODULE = re.compile(r"tests/measurements/test_[a-z0-9_]+\.py")
PYTEST = ("uv", "run", "--no-sync", "pytest")
MATRIX_ARGUMENTS = "${{matrix.pytest-arguments}}"
EVIDENCE_PATH = r"measurements/(?:[A-Za-z0-9_./-]|\$\{\{matrix\.run\}\})+"
PINNED_MEASUREMENT_COMMAND = re.compile(
    rf"(?:KORA_MEASUREMENT_OUTPUT=(?:\$PWD/)?{EVIDENCE_PATH} )?"
    r"(?P<replacing>exec )?"
    r"uv run --no-sync pytest -q(?: -p no:logging)? -m measurement"
    rf"(?: (?P<matrix>{re.escape(MATRIX_ARGUMENTS)}))?"
    rf"(?: --junitxml={EVIDENCE_PATH})?"
    r"(?P<modules>(?: tests/measurements/test_[a-z0-9_]+\.py"
    rf"| {re.escape(SHARDED_MEASUREMENT)}::test_[a-z0-9_]+)*)"
)
AMBIGUOUS_WORD = re.compile(r"[\s'\"\\]")
PREPARED_MEASUREMENT_STEPS = frozenset(
    {
        ("qualification", "lifetime_measurement"),
        (INSTALLED_LIFETIME, "lifetime_measurement"),
    }
)
SOURCE_REVISION_FORMAT = "Actual checkout: %s\\nRequested measurement revision: %s\\n"
UNCANCELLED = "${{ !cancelled() }}"
SCAFFOLD_PLATFORMS = (
    ("ubuntu-24.04", "3.11.17", "sqlite,postgresql"),
    ("ubuntu-24.04", "3.15.0", "sqlite,postgresql"),
    ("macos-15", "3.11.17", "sqlite"),
)
CONFORMANCE_ONLY = "${{ matrix.lane == 'conformance' }}"
LOCAL_RUNTIME_ONLY = "${{ matrix.lane == 'local-runtime' }}"
FRONTEND_ONLY = "${{ matrix.lane != 'local-runtime' }}"
LANE_STEP_CONDITIONS = {
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020": FRONTEND_ONLY,
    "pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413": FRONTEND_ONLY,
    "static_checks": CONFORMANCE_ONLY,
    "preparation": LOCAL_RUNTIME_ONLY,
    "frontend_dependencies": "${{ matrix.lane != 'local-runtime' }}",
    "teardown": "${{ always() && matrix.lane == 'local-runtime' }}",
    "evidence_upload": "${{ always() }}",
    "distributions": CONFORMANCE_ONLY,
    "workflow_cancellation": "${{ cancelled() }}",
    "Require complete lane execution": "${{ always() }}",
}


def required_steps(job: dict[str, object]) -> tuple[str, ...]:
    return tuple(
        identity for identity in steps(job) if identity not in {GATE, CANCELLATION}
    )


def producer_fields(job_id: str) -> tuple[str, ...]:
    identities = required_steps(table(workflow_jobs()[job_id]))
    return tuple(f"{identity.upper()}_OUTCOME" for identity in identities)


def test_measurements_are_independent_of_complete_platform_checks() -> None:
    jobs = workflow_jobs()
    scaffold = table(jobs["scaffold-tests"])
    scaffold_steps = [table(value) for value in cast(list[object], scaffold["steps"])]
    rows = [
        table(value)
        for value in cast(
            list[object], table(table(scaffold["strategy"])["matrix"])["include"]
        )
    ]

    commands = "\n".join(
        str(step.get("run", ""))
        for step in scaffold_steps
        if step.get("id") != "diagnostics"
    )
    diagnostics = next(
        step
        for step in step_list(table(jobs["local-runtime-diagnostics"]))
        if step.get("name") == "Measure local runtime cost"
    )

    assert {(row["os"], row["python"], row["databases"]) for row in rows} == set(
        SCAFFOLD_PLATFORMS
    )
    assert "uv run --no-sync python -m tests.support.ordinary_pytest -q" in commands
    assert "-m measurement" not in commands
    assert table(jobs["local-runtime-diagnostics"])["continue-on-error"] is True
    assert "local-runtime-diagnostics" not in cast(
        list[object], scaffold.get("needs", [])
    )
    assert command_lines(diagnostics["run"]) == [
        ("mkdir", "-p", "measurements"),
        (*PYTEST, "-q", "-m", "measurement", LANE_DIAGNOSTIC_MEASUREMENT),
    ]
    assert MEASUREMENT_MODULE.findall(str(diagnostics["run"])) == [
        LANE_DIAGNOSTIC_MEASUREMENT
    ]
    assert "pricing_command" not in commands
    for required in (
        "uv lock --check",
        "uv run --no-sync ruff format --check .",
        "uv run --no-sync ruff check --ignore-noqa .",
        "uv run --no-sync pyright",
        "pnpm --dir web run typecheck",
        "pnpm --dir web run lint",
        "pnpm --dir web run build",
        "KORA_BUILD_OFFLINE=1 uv build --offline",
        "uv sync --locked --no-build --group build --extra postgres --extra cel",
        "git diff --exit-code -- pyproject.toml uv.lock web/package.json",
    ):
        assert required in commands
    for job_id in PRODUCER_JOBS:
        job = table(jobs[job_id])
        if job_id in {"qualification", INSTALLED_LIFETIME}:
            assert "needs" not in job
        else:
            assert job["needs"] == ["change-scope"]
        assert "strategy" not in job
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["timeout-minutes"] == 10
    assert (
        table(table(jobs["resource-measurements"])["env"])["KORA_TEST_DATABASES"]
        == "sqlite,postgresql"
    )
    assert (
        table(table(jobs["pricing-measurements"])["env"])["KORA_TEST_DATABASES"]
        == "sqlite,postgresql"
    )


def test_producer_jobs_are_exactly_the_jobs_that_publish_a_proof() -> None:
    jobs = workflow_jobs()

    publishing = {
        job_id
        for job_id, value in jobs.items()
        if "verified" in table(table(value).get("outputs", {}))
    }

    assert publishing == set(PRODUCER_JOBS) | {"change-scope", "scaffold-evidence"}


@pytest.mark.parametrize("job_id", PRODUCER_JOBS)
def test_producer_proof_covers_every_required_step(job_id: str) -> None:
    job = table(workflow_jobs()[job_id])
    entries = step_list(job)
    selected = steps(job)

    setup = len(
        list(takewhile(lambda step: "uses" in step and "id" not in step, entries))
    )
    gate = selected[GATE]
    evidence = PRODUCER_EVIDENCE[job_id]

    assert (
        table(job["outputs"])["verified"] == f"${{{{ steps.{GATE}.outputs.verified }}}}"
    )
    assert {*evidence.measurements, *evidence.uploads} <= set(required_steps(job))
    assert all(
        action_name(selected[upload]["uses"]) == "actions/upload-artifact"
        for upload in evidence.uploads
    )
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in entries)
    assert entries[-1] == gate
    assert setup > 0
    assert all("id" in step for step in entries[setup:])
    assert gate["if"] == "${{ always() }}"
    assert selected[CANCELLATION]["if"] == "${{ cancelled() }}"
    assert selected[CANCELLATION]["run"] == "exit 0"
    assert table(gate["env"]) == {
        **{
            f"{identity.upper()}_OUTCOME": f"${{{{ steps.{identity}.outcome }}}}"
            for identity in required_steps(job)
        },
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }


@pytest.mark.parametrize(
    "job_id",
    [
        "resource-measurements",
        "provider-protocol-measurements",
        "pricing-measurements",
        "git-publication-measurements",
        "qualification",
        INSTALLED_LIFETIME,
    ],
)
def test_measurement_jobs_verify_their_own_final_environment(job_id: str) -> None:
    jobs = workflow_jobs()
    selected = steps(table(jobs[job_id]))
    scaffold_steps = cast(list[object], table(jobs["scaffold-tests"])["steps"])
    original = next(
        step["run"]
        for value in scaffold_steps
        if (step := table(value)).get("name")
        == "Check the synced environment and unchanged dependency files"
    )

    final_sync = selected["final_sync"]

    assert final_sync["run"] == original
    assert final_sync["if"] == "${{ always() && !cancelled() }}"
    assert list(selected).index("final_sync") < list(selected).index(
        "required_measurements"
    )
    assert all(
        list(selected).index(identity) < list(selected).index("final_sync")
        for identity in selected
        if identity.endswith("_upload")
    )


def test_full_disk_route_preserves_private_evidence_before_disposal() -> None:
    selected = steps(table(workflow_jobs()["resource-measurements"]))
    required = (
        "database_upload",
        "full_disk_prepare",
        "full_disk",
        "full_disk_evidence",
        "full_disk_cleanup",
        "full_disk_upload",
        "final_sync",
    )
    upload = selected["full_disk_upload"]
    evidence = str(table(upload["with"])["path"])

    positions = [list(selected).index(identity) for identity in required]
    preparation = command_lines(selected["full_disk_prepare"]["run"])
    capture = command_lines(selected["full_disk_evidence"]["run"])
    mount = preparation[3]
    options = set(mount[5].split(","))

    assert positions == sorted(positions)
    assert [*preparation[:3], *preparation[4:]] == [
        ("mkdir", "-m", "700", evidence),
        ("KORA_FULL_DISK_MOUNT=$(mktemp -d $RUNNER_TEMP/kora-full-disk.XXXXXX)",),
        (
            "printf",
            "KORA_LIFETIME_FULL_DISK_ROOT=%s\\n",
            "$KORA_FULL_DISK_MOUNT",
            ">>",
            "$GITHUB_ENV",
        ),
        (
            "findmnt",
            "-n",
            "--output",
            "SOURCE,FSTYPE,TARGET,OPTIONS",
            "--target",
            "$KORA_FULL_DISK_MOUNT",
            ">",
            f"{evidence}/mount.txt",
        ),
    ]
    assert (*mount[:5], *mount[6:]) == (
        "sudo",
        "mount",
        "-t",
        "tmpfs",
        "-o",
        "tmpfs",
        "$KORA_FULL_DISK_MOUNT",
    )
    assert {"mode=0700", "nodev", "nosuid"} <= options
    assert any(option.startswith("size=") for option in options)
    assert command_lines(selected["full_disk"]["run"]) == [
        (
            *PYTEST,
            "-q",
            f"--junitxml={evidence}/tests.xml",
            "tests/conformance/lifetime_full_disk.py",
        )
    ]
    assert capture == [
        ("test", "$FULL_DISK_PREPARE_OUTCOME", "=", "success"),
        (
            "printf",
            SOURCE_REVISION_FORMAT,
            "$KORA_LIFETIME_CHECKOUT_REVISION",
            "$KORA_MEASUREMENT_REVISION",
            ">",
            f"{evidence}/source-revision.txt",
        ),
        (
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "tests.measurements.lifetime_evidence",
            "full-disk",
            "$KORA_LIFETIME_FULL_DISK_ROOT",
            f"$PWD/{evidence}",
        ),
    ]
    assert command_lines(selected["full_disk_cleanup"]["run"]) == [
        ("test", "$FULL_DISK_EVIDENCE_OUTCOME", "=", "success"),
        ("test", "-n", "$KORA_LIFETIME_FULL_DISK_ROOT"),
        ("sudo", "umount", "$KORA_LIFETIME_FULL_DISK_ROOT"),
        ("rmdir", "$KORA_LIFETIME_FULL_DISK_ROOT"),
    ]
    assert table(selected["full_disk_cleanup"]["env"]) == {
        "FULL_DISK_EVIDENCE_OUTCOME": "${{ steps.full_disk_evidence.outcome }}"
    }
    for identity in ("full_disk_evidence", "full_disk_cleanup", "full_disk_upload"):
        assert selected[identity]["if"] == (
            "${{ always() && !cancelled() && "
            "steps.full_disk_prepare.outcome != 'skipped' }}"
        )
    environment = table(selected["full_disk_evidence"]["env"])
    assert environment["KORA_LIFETIME_CHECKOUT_REVISION"] == (
        "${{ steps.source_revision.outputs.checkout_revision }}"
    )
    assert environment["KORA_MEASUREMENT_REVISION"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert action_name(upload["uses"]) == "actions/upload-artifact"
    assert table(upload["with"])["if-no-files-found"] == "error"


def test_lifetime_qualification_is_separate_and_preserves_its_evidence() -> None:
    document = workflow_document("conformance-lifetime.yml")
    jobs = table(document["jobs"])
    job = table(jobs["qualification"])
    selected = steps(job)
    upload = selected["lifetime_upload"]
    evidence = str(table(upload["with"])["path"])

    triggers = table(document["on"])
    schedule = [table(entry) for entry in cast(list[object], triggers["schedule"])]
    order = [
        list(selected).index(identity)
        for identity in (
            "lifetime_measurement",
            "lifetime_evidence",
            "lifetime_upload",
            "final_sync",
        )
    ]
    measurement = command_lines(selected["lifetime_measurement"]["run"])
    capture = command_lines(selected["lifetime_evidence"]["run"])

    assert set(triggers) == {"workflow_dispatch", "schedule"}
    assert triggers["workflow_dispatch"] is None
    assert schedule
    assert all(isinstance(entry["cron"], str) for entry in schedule)
    assert document["permissions"] == {"contents": "read"}
    assert set(jobs) == {"qualification", INSTALLED_LIFETIME, "lifetime-evidence"}
    assert not set(jobs) & set(table(workflow_document()["jobs"]))
    assert "qualification" not in cast(
        list[str], table(workflow_jobs()["cel-measurements"])["needs"]
    )
    assert order == sorted(order)
    assert "lifetime_measurement" not in steps(
        table(workflow_jobs()["resource-measurements"])
    )
    assert (
        selected["dependencies"]["run"]
        == steps(table(workflow_jobs()["resource-measurements"]))["dependencies"]["run"]
    )
    assert measurement == [
        ("mkdir", "-p", str(Path(evidence).parent)),
        ("mkdir", "-m", "700", evidence),
        (
            "printf",
            SOURCE_REVISION_FORMAT,
            "$KORA_LIFETIME_CHECKOUT_REVISION",
            "$KORA_MEASUREMENT_REVISION",
            ">",
            f"{evidence}/source-revision.txt",
        ),
        (
            f"KORA_MEASUREMENT_OUTPUT=$PWD/{evidence}/report.json",
            "exec",
            *PYTEST,
            "-q",
            "-p",
            "no:logging",
            "-m",
            "measurement",
            f"--junitxml={evidence}/tests.xml",
            f"{SHARDED_MEASUREMENT}::test_conformance_fixture_resources_are_released",
        ),
    ]
    assert [line[:-1] for line in capture] == [
        (
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "tests.measurements.lifetime_evidence",
            "cohorts",
            f"$PWD/{evidence}",
            "$KORA_LIFETIME_MEASUREMENT_OUTCOME",
        )
    ]
    assert table(selected["lifetime_evidence"]["env"]) == {
        "KORA_LIFETIME_MEASUREMENT_OUTCOME": (
            "${{ steps.lifetime_measurement.outcome }}"
        )
    }
    for identity in ("lifetime_evidence", "lifetime_upload"):
        assert selected[identity]["if"] == (
            "${{ always() && steps.lifetime_measurement.outcome != 'skipped' }}"
        )
    environment = table(selected["lifetime_measurement"]["env"])
    assert environment["KORA_LIFETIME_CHECKOUT_REVISION"] == (
        "${{ steps.source_revision.outputs.checkout_revision }}"
    )
    assert environment["KORA_MEASUREMENT_REVISION"] == "${{ github.sha }}"
    assert action_name(upload["uses"]) == "actions/upload-artifact"
    assert (
        table(upload["with"])["name"]
        == "conformance-lifetime-${{ github.sha }}-fixture"
    )
    assert table(upload["with"])["if-no-files-found"] == "error"
    assert table(upload["with"])["include-hidden-files"] is True


def run_lifetime_preparation(
    preparation: str, workspace: Path, checkout: str, requested: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/bin/bash", "-e", "-c", preparation],
        cwd=workspace,
        env={
            **os.environ,
            "KORA_LIFETIME_CHECKOUT_REVISION": checkout,
            "KORA_MEASUREMENT_REVISION": requested,
        },
        capture_output=True,
        check=False,
        timeout=5,
    )


LIFETIME_SHARDS = [
    (job_id, row)
    for job_id in ("qualification", INSTALLED_LIFETIME)
    for row in matrix_rows(table(workflow_jobs()[job_id]))
]


@pytest.mark.parametrize(("job_id", "row"), LIFETIME_SHARDS)
def test_lifetime_measurement_prepares_private_evidence_in_a_fresh_checkout(
    tmp_path: Path, job_id: str, row: dict[str, object]
) -> None:
    selected = steps(table(workflow_jobs()[job_id]))
    run = rendered(selected["lifetime_measurement"]["run"], row)
    preparation = run[: run.index("KORA_MEASUREMENT_OUTPUT=")]
    upload = table(selected["lifetime_upload"]["with"])
    evidence = tmp_path / rendered(upload["path"], row)

    fresh = run_lifetime_preparation(preparation, tmp_path, "a" * 40, "b" * 40)
    repeated = run_lifetime_preparation(preparation, tmp_path, "c" * 40, "d" * 40)

    assert (fresh.returncode, fresh.stderr) == (0, b"")
    assert repeated.returncode != 0
    assert repeated.stderr != b""
    observed = evidence.lstat()
    assert stat.S_ISDIR(observed.st_mode)
    assert stat.S_IMODE(observed.st_mode) == 0o700
    assert (evidence / "source-revision.txt").read_text() == (
        f"Actual checkout: {'a' * 40}\nRequested measurement revision: {'b' * 40}\n"
    )


@pytest.mark.parametrize(
    ("job_id", "field", "outcome"),
    [(job_id, "WORKFLOW_CANCELLED", "false") for job_id in PRODUCER_JOBS]
    + [
        (job_id, field, outcome)
        for job_id in PRODUCER_JOBS
        for field in producer_fields(job_id)
        for outcome in ("failure", "cancelled", "skipped", "")
    ]
    + [
        (job_id, "WORKFLOW_CANCELLED", outcome)
        for job_id in PRODUCER_JOBS
        for outcome in ("true", "")
    ],
)
def test_producer_shell_emits_proof_only_after_complete_success(
    tmp_path: Path, job_id: str, field: str, outcome: str
) -> None:
    gate = steps(table(workflow_jobs()[job_id]))[GATE]
    output = tmp_path / "outputs"
    environment = {
        **os.environ,
        **dict.fromkeys(producer_fields(job_id), "success"),
        "WORKFLOW_CANCELLED": "false",
        "GITHUB_OUTPUT": str(output),
        field: outcome,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    passed = field == "WORKFLOW_CANCELLED" and outcome == "false"
    assert (result.returncode == 0) is passed
    assert (output.read_text() if output.exists() else "") == (
        "verified=true\n" if passed else ""
    )


def test_pricing_route_preserves_interpreters_publication_and_release() -> None:
    job = table(workflow_jobs()["pricing-measurements"])
    selected = steps(job)
    entries = step_list(job)

    provisioning = command_lines(selected["pricing_environment"]["run"])
    support = "from tests.measurements.pricing_lifetime_support import"

    assert (
        "export",
        "UV_PROJECT_ENVIRONMENT=$KORA_PRICING_ROOT/environment",
    ) in provisioning
    assert any("--require-hashes" in line for line in provisioning)
    assert any("--require-build-hashes" in line for line in provisioning)
    assert command_lines(selected["pricing_measurement"]["run"]) == [
        (
            "$KORA_PRICING_ROOT/environment/bin/python",
            "-c",
            f"import sys; {support} pricing_command; sys.exit(pricing_command())",
        )
    ]
    assert (
        selected["pricing_isolation"]["if"]
        == "${{ !cancelled() && steps.pricing_environment.outcome != 'skipped' }}"
    )
    assert (
        selected["pricing_release"]["if"]
        == "${{ always() && steps.pricing_environment.outcome != 'skipped' }}"
    )
    assert (
        selected["pricing_publication"]["if"]
        == "${{ !cancelled() && steps.pricing_environment.outcome != 'skipped' }}"
    )
    assert (
        selected["pricing_upload"]["if"]
        == "${{ !cancelled() && steps.pricing_publication.outputs.ready == 'true' }}"
    )
    assert (
        selected["pricing_publication_release"]["if"]
        == "${{ always() && steps.pricing_publication.outputs.identity != '' }}"
    )
    assert selected["final_sync"]["if"] == "${{ always() && !cancelled() }}"
    assert table(selected["pricing_publication"]["env"]) == {
        "KORA_PRICING_MEASUREMENT_OUTCOME": "${{ steps.pricing_measurement.outcome }}",
        "KORA_PRICING_RELEASE_OUTCOME": "${{ steps.pricing_release.outcome }}",
        "KORA_PRICING_EVIDENCE": (
            "${{ github.workspace }}/measurements/pricing-database-lifetime"
        ),
    }
    assert table(selected["pricing_upload"]["with"])["if-no-files-found"] == "error"
    for identity, operation in (
        ("pricing_release", "pricing_environment_release"),
        ("pricing_publication_release", "pricing_publication_release"),
    ):
        assert command_lines(selected[identity]["run"]) == [
            ("$PWD/.venv/bin/python", "-c", f"{support} {operation}; {operation}()")
        ]
    assert all("matrix." not in str(entry) for entry in entries)
    assert list(selected).index("pricing_publication_release") < list(selected).index(
        "final_sync"
    )


def measurement_modules() -> frozenset[str]:
    return frozenset(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests" / "measurements").glob("test_*.py")
        if "pytest.mark.measurement" in path.read_text(encoding="utf-8")
    )


MeasurementLine = tuple[str, dict[str, object], tuple[str, ...], tuple[str, ...]]


def measurement_lines() -> list[MeasurementLine]:
    found: list[MeasurementLine] = []
    for job_id, value in workflow_jobs().items():
        job = table(value)
        arguments = tuple(
            str(row.get("pytest-arguments", "")) for row in matrix_rows(job)
        )
        if job_id == "local-runtime-diagnostics":
            continue
        for step in step_list(job):
            for line in command_lines(step.get("run", "")):
                if "pytest" in line and selects_measurements(line):
                    found.append((job_id, step, line, arguments))
    return found


def selects_measurements(line: tuple[str, ...]) -> bool:
    return ("-m", "measurement") in zip(line, line[1:], strict=False)


def selected_modules(line: tuple[str, ...], arguments: tuple[str, ...]) -> set[str]:
    matrix = arguments if MATRIX_ARGUMENTS in line else ()
    return set(MEASUREMENT_MODULE.findall(" ".join((*line, *matrix))))


def is_pinned_measurement_command(
    line: tuple[str, ...], *, replaces_shell: bool
) -> bool:
    pinned = PINNED_MEASUREMENT_COMMAND.fullmatch(" ".join(line))
    return (
        pinned is not None
        and not any(AMBIGUOUS_WORD.search(word) for word in line)
        and bool(pinned["matrix"] or pinned["modules"])
        and (pinned["replacing"] is not None) is replaces_shell
    )


@pytest.mark.parametrize(
    ("selection", "pinned"),
    [
        (SHARDED_MEASUREMENT, True),
        (f"{SHARDED_MEASUREMENT}::test_installed_listing_resources_are_released", True),
        ("tests/measurements/test_protocol_models_cost.py", True),
        (
            "tests/measurements/test_protocol_models_cost.py::test_protocol_import_cost",
            False,
        ),
        (
            f"{SHARDED_MEASUREMENT} "
            "tests/measurements/test_logging_overhead.py::test_logging_overhead",
            False,
        ),
    ],
)
def test_only_the_sharded_module_may_be_routed_by_test_node(
    selection: str, pinned: bool
) -> None:
    command = f"uv run --no-sync pytest -q -p no:logging -m measurement {selection}"

    accepted = is_pinned_measurement_command(
        command_lines(command)[0], replaces_shell=False
    )

    assert accepted is pinned


@pytest.mark.parametrize(
    ("prefix", "replaces_shell", "pinned"),
    [
        ("exec ", True, True),
        ("", True, False),
        ("exec ", False, False),
        ("", False, True),
    ],
)
def test_only_lifetime_measurements_replace_their_step_shell(
    prefix: str, replaces_shell: bool, pinned: bool
) -> None:
    command = (
        "KORA_MEASUREMENT_OUTPUT=$PWD/measurements/conformance-lifetime/report.json "
        f"{prefix}uv run --no-sync pytest -q -p no:logging -m measurement "
        f"{SHARDED_MEASUREMENT}::test_conformance_fixture_resources_are_released"
    )

    accepted = is_pinned_measurement_command(
        command_lines(command)[0], replaces_shell=replaces_shell
    )

    assert accepted is pinned


def is_the_only_command(job_id: str, step: dict[str, object]) -> bool:
    return (job_id, step.get("id")) in PREPARED_MEASUREMENT_STEPS or len(
        command_lines(step["run"])
    ) == 1


def is_gated_measurement_step(job_id: str, step: dict[str, object]) -> bool:
    if job_id == MATRIX_PRODUCER:
        return "id" in step
    return job_id in GATED_JOBS and step.get("id") in required_steps(
        table(workflow_jobs()[job_id])
    )


def test_original_resource_and_cel_commands_keep_their_artifacts() -> None:
    jobs = workflow_jobs()
    cel = steps(table(jobs["cel-producer"]))
    routed_elsewhere = {
        COMMAND_ROUTED_MEASUREMENT,
        STORAGE_WORKFLOW_MEASUREMENT,
        LANE_DIAGNOSTIC_MEASUREMENT,
    }

    lines = measurement_lines()
    routes = [
        module
        for _, _, line, arguments in lines
        for module in selected_modules(line, arguments)
    ]

    assert all(
        is_pinned_measurement_command(line, replaces_shell=job_id in LIFETIME_JOBS)
        for job_id, _, line, _ in lines
    )
    assert sorted(routes) == sorted(
        [*(measurement_modules() - routed_elsewhere), SHARDED_MEASUREMENT]
    )
    assert all(is_gated_measurement_step(job_id, step) for job_id, step, _, _ in lines)
    assert all(is_the_only_command(job_id, step) for job_id, step, _, _ in lines)
    assert all(
        "continue-on-error" not in step
        and "continue-on-error" not in table(jobs[job_id])
        for job_id, step, _, _ in lines
    )
    assert all(step.get("if", UNCANCELLED) == UNCANCELLED for _, step, _, _ in lines)
    assert routed_elsewhere <= measurement_modules()
    assert cel["measurement"]["if"] == UNCANCELLED
    assert cel["cost_regression"]["if"] == UNCANCELLED
    assert cel["cel_artifacts"]["if"] == "${{ always() && !cancelled() }}"
    for identity in ("cost_upload", "lifetime_upload"):
        assert table(cel[identity]["with"])["if-no-files-found"] == "error"
        assert (
            "measurements/cel-checkout-revision.txt"
            in str(table(cel[identity]["with"])["path"]).split()
        )


def test_join_keeps_cel_receipts_separate_and_requires_every_download() -> None:
    jobs = workflow_jobs()
    join = steps(table(jobs["cel-measurements"]))
    producer = steps(table(jobs["cel-producer"]))

    lifetime = table(join["cel_lifetime_download"]["with"])
    cost = table(join["cel_cost_download"]["with"])
    shards = table(join["ir_download"]["with"])
    union = table(join["ir_union_upload"]["with"])

    assert lifetime["name"] == table(producer["lifetime_upload"]["with"])["name"]
    assert cost["name"] == table(producer["cost_upload"]["with"])["name"]
    assert lifetime["path"] != cost["path"]
    assert command_lines(join["ir_inventory"]["run"]) == [
        (
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "tests.measurements.ir_measurement_evidence",
            str(shards["path"]),
            "$KORA_MEASUREMENT_REVISION",
            ">",
            str(union["path"]),
        )
    ]


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
    selected = steps(table(workflow_jobs()["cel-measurements"]))
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
        ["/bin/bash", "-e", "-c", str(selected["cel_inventory"]["run"])],
        cwd=tmp_path,
        env={**os.environ, "KORA_MEASUREMENT_REVISION": revision},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (mutation == "valid")


def step_key(step: dict[str, object]) -> str:
    return str(step.get("id", step.get("name", step.get("uses"))))


def test_scaffold_lanes_keep_fail_closed_settings_and_pinned_steps() -> None:
    document = workflow_document()
    job = table(table(document["jobs"])["scaffold-tests"])
    job_steps = [table(value) for value in cast(list[object], job["steps"])]

    conditions = {step_key(step): step["if"] for step in job_steps if "if" in step}

    assert job["runs-on"] == "${{ matrix.os }}"
    assert table(job["env"])["KORA_SCAFFOLD_LANE"] == "${{ matrix.lane }}"
    assert job["if"] == "${{ needs.change-scope.outputs.full != 'false' }}"
    assert job["needs"] == ["change-scope"]
    assert "continue-on-error" not in job
    assert "permissions" not in job
    assert document["permissions"] == {"contents": "read"}
    assert conditions == LANE_STEP_CONDITIONS
    assert [step_key(step) for step in job_steps if "continue-on-error" in step] == []
    checkout = job_steps[0]
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert table(checkout["with"]) == {"persist-credentials": False}


def test_scaffold_join_keeps_the_required_names() -> None:
    job = table(workflow_jobs()["scaffold"])

    names = [rendered(job["name"], entry) for entry in matrix_rows(job)]

    assert names == [
        "Scaffold (ubuntu-24.04, Python 3.11.17)",
        "Scaffold (ubuntu-24.04, Python 3.15.0)",
        "Scaffold (macos-15, Python 3.11.17)",
    ]
    assert "permissions" not in job
    assert "continue-on-error" not in job
