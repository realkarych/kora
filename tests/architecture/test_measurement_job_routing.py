import os
import re
import subprocess
from pathlib import Path
from typing import Protocol, cast

import pytest
from ruamel.yaml import YAML

PRODUCER_STEPS = {
    "resource-measurements": (
        "dependencies",
        "source_revision",
        "measurement",
        "logging_upload",
        "database_measurement",
        "database_upload",
        "provider_loading_measurement",
        "provider_upload",
        "protocol_models_measurement",
        "protocol_upload",
        "full_disk_prepare",
        "full_disk",
        "full_disk_evidence",
        "full_disk_cleanup",
        "full_disk_upload",
        "final_sync",
    ),
    "qualification": (
        "dependencies",
        "source_revision",
        "lifetime_measurement",
        "lifetime_evidence",
        "lifetime_upload",
        "final_sync",
    ),
    "pricing-measurements": (
        "dependencies",
        "source_revision",
        "pricing_environment",
        "pricing_measurement",
        "pricing_isolation",
        "pricing_release",
        "pricing_publication",
        "pricing_upload",
        "pricing_publication_release",
        "final_sync",
    ),
    "git-publication-measurements": (
        "dependencies",
        "source_revision",
        "git_publication_measurement",
        "git_publication_upload",
        "final_sync",
    ),
    "cel-producer": (
        "dependencies",
        "source_revision",
        "measurement",
        "cost_regression",
        "cel_artifacts",
        "cost_upload",
        "lifetime_upload",
    ),
}
SCAFFOLD_PLATFORMS = (
    ("ubuntu-24.04", "3.11.17", "sqlite,postgresql"),
    ("ubuntu-24.04", "3.15.0", "sqlite,postgresql"),
    ("macos-15", "3.11.17", "sqlite"),
)
GENERAL_LANE_ARGUMENTS = (
    "--ignore=tests/plugins/storage "
    "--ignore=tests/conformance/test_artifact_storage_conformance.py"
)
SCAFFOLD_LANES = (
    ("conformance", GENERAL_LANE_ARGUMENTS),
    ("remainder", GENERAL_LANE_ARGUMENTS),
    (
        "storage",
        "tests/plugins/storage tests/conformance/test_artifact_storage_conformance.py",
    ),
)
REMAINDER_ONLY = "${{ matrix.lane == 'remainder' }}"
NOT_STORAGE = "${{ matrix.lane != 'storage' }}"
LANE_STEP_CONDITIONS = {
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020": NOT_STORAGE,
    "pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413": NOT_STORAGE,
    "frontend_dependencies": NOT_STORAGE,
    "static_checks": REMAINDER_ONLY,
    "evidence_upload": "${{ always() }}",
    "distributions": REMAINDER_ONLY,
    "workflow_cancellation": "${{ cancelled() }}",
    "Require complete lane execution": "${{ always() }}",
}
PINNED_ACTION = re.compile(r"[a-z0-9-]+/[a-z0-9-]+@[0-9a-f]{40}")


def table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


def workflow_document(filename: str = "ci.yml") -> dict[str, object]:
    document = cast(YamlLoader, YAML(typ="safe")).load(
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / filename
    )
    return table(document)


def workflow_jobs() -> dict[str, object]:
    return {
        **table(workflow_document()["jobs"]),
        **table(workflow_document("conformance-lifetime.yml")["jobs"]),
    }


def steps(job: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = cast(list[object], job["steps"])
    return {
        str(entry["id"]): entry for value in entries if "id" in (entry := table(value))
    }


def test_measurements_are_independent_of_complete_platform_checks() -> None:
    jobs = workflow_jobs()
    scaffold = table(jobs["scaffold-tests"])
    scaffold_steps = [table(value) for value in cast(list[object], scaffold["steps"])]

    commands = "\n".join(str(step.get("run", "")) for step in scaffold_steps)

    assert table(table(scaffold["strategy"])["matrix"])["include"] == [
        {
            "os": operating_system,
            "python": python,
            "databases": databases,
            "lane": lane,
            "pytest-arguments": arguments,
        }
        for operating_system, python, databases in SCAFFOLD_PLATFORMS
        for lane, arguments in SCAFFOLD_LANES
    ]
    assert "uv run --no-sync python -m tests.support.ordinary_pytest -q" in commands
    assert "-m measurement" not in commands
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
    for job_id in PRODUCER_STEPS:
        job = table(jobs[job_id])
        assert "needs" not in job
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


@pytest.mark.parametrize("job_id", PRODUCER_STEPS)
def test_producer_proof_covers_every_required_step(job_id: str) -> None:
    job = table(workflow_jobs()[job_id])
    selected = steps(job)
    required = PRODUCER_STEPS[job_id]

    gate = selected["required_measurements"]

    assert (
        table(job["outputs"])["verified"]
        == "${{ steps.required_measurements.outputs.verified }}"
    )
    assert table(cast(list[object], job["steps"])[-1]) == gate
    assert gate["if"] == "${{ always() }}"
    assert selected["workflow_cancellation"]["if"] == "${{ cancelled() }}"
    assert selected["workflow_cancellation"]["run"] == "exit 0"
    assert table(gate["env"]) == {
        **{
            f"{identity.upper()}_OUTCOME": f"${{{{ steps.{identity}.outcome }}}}"
            for identity in required
        },
        "WORKFLOW_CANCELLED": "${{ steps.workflow_cancellation.outcome != 'skipped' }}",
    }
    assert set(required) <= selected.keys()


@pytest.mark.parametrize(
    "job_id",
    [
        "resource-measurements",
        "pricing-measurements",
        "git-publication-measurements",
        "qualification",
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
        "protocol_upload",
        "full_disk_prepare",
        "full_disk",
        "full_disk_evidence",
        "full_disk_cleanup",
        "full_disk_upload",
        "final_sync",
    )

    positions = [list(selected).index(identity) for identity in required]

    assert positions == sorted(positions)
    preparation = str(selected["full_disk_prepare"]["run"])
    assert 'mktemp -d "$RUNNER_TEMP/kora-full-disk.XXXXXX"' in preparation
    assert preparation.index('>> "$GITHUB_ENV"') < preparation.index("sudo mount")
    assert (
        'sudo mount -t tmpfs -o "size=16m,uid=$(id -u),gid=$(id -g),'
        'mode=0700,nodev,nosuid" tmpfs "$KORA_FULL_DISK_MOUNT"'
    ) in preparation
    assert selected["full_disk"]["run"] == (
        "uv run --no-sync pytest -q "
        "--junitxml=measurements/conformance-full-disk/tests.xml "
        "tests/conformance/lifetime_full_disk.py\n"
    )
    assert selected["full_disk_cleanup"]["run"] == (
        'test "$FULL_DISK_EVIDENCE_OUTCOME" = success\n'
        'test -n "$KORA_LIFETIME_FULL_DISK_ROOT"\n'
        'sudo umount "$KORA_LIFETIME_FULL_DISK_ROOT"\n'
        'rmdir "$KORA_LIFETIME_FULL_DISK_ROOT"\n'
    )
    assert table(selected["full_disk_cleanup"]["env"]) == {
        "FULL_DISK_EVIDENCE_OUTCOME": "${{ steps.full_disk_evidence.outcome }}"
    }
    assert str(selected["full_disk_evidence"]["run"]).startswith(
        'test "$FULL_DISK_PREPARE_OUTCOME" = success\n'
    )
    assert (
        "uv run --no-sync python -m tests.measurements.lifetime_evidence "
        'full-disk "$KORA_LIFETIME_FULL_DISK_ROOT" '
        '"$PWD/measurements/conformance-full-disk"\n'
    ) in str(selected["full_disk_evidence"]["run"])
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
    assert table(selected["full_disk_upload"]["with"]) == {
        "name": "conformance-full-disk-evidence",
        "path": "measurements/conformance-full-disk",
        "if-no-files-found": "error",
    }
    assert selected["full_disk_upload"]["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )


def test_lifetime_qualification_is_separate_and_preserves_its_evidence() -> None:
    document = workflow_document("conformance-lifetime.yml")
    jobs = table(document["jobs"])
    job = table(jobs["qualification"])
    selected = steps(job)

    positions = [
        list(selected).index(identity) for identity in PRODUCER_STEPS["qualification"]
    ]

    assert table(document["on"]) == {
        "workflow_dispatch": None,
        "schedule": [{"cron": "0 2 * * *"}],
    }
    assert document["permissions"] == {"contents": "read"}
    assert set(jobs) == {"qualification"}
    assert "qualification" not in table(workflow_document()["jobs"])
    assert "qualification" not in cast(
        list[str], table(workflow_jobs()["cel-measurements"])["needs"]
    )
    assert positions == sorted(positions)
    assert "lifetime_measurement" not in steps(
        table(workflow_jobs()["resource-measurements"])
    )
    assert (
        selected["dependencies"]["run"]
        == steps(table(workflow_jobs()["resource-measurements"]))["dependencies"]["run"]
    )
    assert (
        'KORA_MEASUREMENT_OUTPUT="$PWD/measurements/conformance-lifetime/report.json"'
        " \\\n"
        "  uv run --no-sync pytest -q -p no:logging -m measurement "
        "--junitxml=measurements/conformance-lifetime/tests.xml "
        "tests/measurements/test_conformance_lifetime.py\n"
    ) in str(selected["lifetime_measurement"]["run"])
    assert selected["lifetime_evidence"]["run"] == (
        "uv run --no-sync python -m tests.measurements.lifetime_evidence "
        'cohorts "$PWD/measurements/conformance-lifetime" '
        '"$KORA_LIFETIME_MEASUREMENT_OUTCOME" /tmp\n'
    )
    assert table(selected["lifetime_evidence"]["env"]) == {
        "KORA_LIFETIME_MEASUREMENT_OUTCOME": (
            "${{ steps.lifetime_measurement.outcome }}"
        )
    }
    for identity in ("lifetime_evidence", "lifetime_upload"):
        assert selected[identity]["if"] == (
            "${{ always() && !cancelled() && "
            "steps.lifetime_measurement.outcome != 'skipped' }}"
        )
    environment = table(selected["lifetime_measurement"]["env"])
    assert environment["KORA_LIFETIME_CHECKOUT_REVISION"] == (
        "${{ steps.source_revision.outputs.checkout_revision }}"
    )
    assert environment["KORA_MEASUREMENT_REVISION"] == "${{ github.sha }}"
    assert selected["lifetime_upload"]["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )
    assert table(selected["lifetime_upload"]["with"]) == {
        "name": "conformance-lifetime-measurement",
        "path": "measurements/conformance-lifetime",
        "if-no-files-found": "error",
        "include-hidden-files": True,
    }


@pytest.mark.parametrize(
    ("job_id", "field", "outcome"),
    [(job_id, "WORKFLOW_CANCELLED", "false") for job_id in PRODUCER_STEPS]
    + [
        (job_id, f"{identity.upper()}_OUTCOME", outcome)
        for job_id, required in PRODUCER_STEPS.items()
        for identity in required
        for outcome in ("failure", "cancelled", "skipped", "")
    ]
    + [
        (job_id, "WORKFLOW_CANCELLED", outcome)
        for job_id in PRODUCER_STEPS
        for outcome in ("true", "")
    ],
)
def test_producer_shell_emits_proof_only_after_complete_success(
    tmp_path: Path, job_id: str, field: str, outcome: str
) -> None:
    gate = steps(table(workflow_jobs()[job_id]))["required_measurements"]
    output = tmp_path / "outputs"
    environment = {
        **os.environ,
        **{
            f"{identity.upper()}_OUTCOME": "success"
            for identity in PRODUCER_STEPS[job_id]
        },
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
    entries = [table(value) for value in cast(list[object], job["steps"])]

    environment = str(selected["pricing_environment"]["run"])

    assert table(entries[1]["with"])["python-version"] == "3.15.0"
    assert "uv python install 3.11.17" in environment
    assert "sys.version_info[:3] == (3, 15, 0)" in environment
    assert "sys.version_info[:3] == (3, 11, 17)" in environment
    assert (
        'export UV_PROJECT_ENVIRONMENT="$KORA_PRICING_ROOT/environment"' in environment
    )
    assert "--require-hashes" in environment
    assert "--require-build-hashes --editable ." in environment
    assert "original-environment.sha256" in environment
    assert "original-packages.txt" in environment
    assert "dependencies.sha256" in environment
    assert selected["pricing_measurement"]["run"] == (
        '"$KORA_PRICING_ROOT/environment/bin/python" -c \\\n'
        "  'import sys; from tests.measurements.pricing_lifetime_support "
        "import pricing_command; sys.exit(pricing_command())'\n"
    )
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
    assert "pricing_publication_release()" in str(
        selected["pricing_publication_release"]["run"]
    )
    assert "pricing_environment_release()" in str(selected["pricing_release"]["run"])
    assert all("matrix." not in str(entry) for entry in entries)
    assert list(selected).index("pricing_publication_release") < list(selected).index(
        "final_sync"
    )


def test_original_resource_and_cel_commands_keep_their_artifacts() -> None:
    jobs = workflow_jobs()
    resource = steps(table(jobs["resource-measurements"]))
    cel = steps(table(jobs["cel-producer"]))

    resource_commands = {
        identity: str(resource[identity]["run"])
        for identity in (
            "measurement",
            "database_measurement",
            "provider_loading_measurement",
            "protocol_models_measurement",
        )
    }

    for identity, filename, logging in (
        ("measurement", "test_logging_overhead.py", " -p no:logging"),
        ("database_measurement", "test_metadata_database_lifetime.py", ""),
        (
            "provider_loading_measurement",
            "test_provider_loading_cost.py",
            " -p no:logging",
        ),
        (
            "protocol_models_measurement",
            "test_protocol_models_cost.py",
            " -p no:logging",
        ),
    ):
        assert (
            f"uv run --no-sync pytest -q{logging} -m measurement "
            f"tests/measurements/{filename}\n" in resource_commands[identity]
        )
        assert "if" not in resource[identity]
    assert (
        cel["measurement"]["run"]
        == "uv run --no-sync pytest -q -p no:logging -m measurement "
        "tests/measurements/test_cel_evaluator_lifetime.py"
    )
    assert (
        cel["cost_regression"]["run"]
        == "uv run --no-sync pytest -q -p no:logging -m measurement "
        "tests/measurements/test_cel_cost_regression.py"
    )
    assert cel["measurement"]["if"] == "${{ !cancelled() }}"
    assert cel["cost_regression"]["if"] == "${{ !cancelled() }}"
    assert cel["cel_artifacts"]["if"] == "${{ always() && !cancelled() }}"
    for identity in ("cost_upload", "lifetime_upload"):
        assert table(cel[identity]["with"])["if-no-files-found"] == "error"
        assert "measurements/cel-checkout-revision.txt" in str(
            table(cel[identity]["with"])["path"]
        )


def test_join_keeps_cel_receipts_separate_and_requires_every_download() -> None:
    selected = steps(table(workflow_jobs()["cel-measurements"]))

    lifetime = table(selected["cel_lifetime_download"]["with"])
    cost = table(selected["cel_cost_download"]["with"])

    assert lifetime == {
        "name": "cel-evaluator-lifetime-measurement",
        "path": "measurements/cel-lifetime",
    }
    assert cost == {
        "name": "cel-cost-regression-measurement",
        "path": "measurements/cel-cost",
    }
    assert (
        selected["cel_inventory"]["if"] == "${{ always() && !cancelled() "
        "&& steps.cel_lifetime_download.outcome == 'success' "
        "&& steps.cel_cost_download.outcome == 'success' }}"
    )
    assert selected["ir_inventory"]["run"] == (
        "uv run --no-sync python -m tests.measurements.ir_measurement_evidence \\\n"
        '  measurements/ir-shards "$KORA_MEASUREMENT_REVISION" \\\n'
        "  > measurements/ir-shard-union.json\n"
    )


@pytest.mark.parametrize("arm", ["lifetime", "cost"])
@pytest.mark.parametrize(
    "mutation",
    ["valid", "missing", "empty", "extra", "revision", "symlink", "directory"],
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
    assert "if" not in job
    assert "needs" not in job
    assert "continue-on-error" not in job
    assert "permissions" not in job
    assert document["permissions"] == {"contents": "read"}
    assert conditions == LANE_STEP_CONDITIONS
    assert all("continue-on-error" not in step for step in job_steps)
    assert all(
        PINNED_ACTION.fullmatch(str(step["uses"])) is not None
        for step in job_steps
        if "uses" in step
    )
    checkout = job_steps[0]
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert table(checkout["with"]) == {"persist-credentials": False}


def test_scaffold_join_keeps_the_required_names() -> None:
    job = table(workflow_jobs()["scaffold"])
    include = cast(
        list[dict[str, str]], table(table(job["strategy"])["matrix"])["include"]
    )

    rendered = [
        str(job["name"])
        .replace("${{ matrix.os }}", entry["os"])
        .replace("${{ matrix.python }}", entry["python"])
        for entry in include
    ]

    assert rendered == [
        "Scaffold (ubuntu-24.04, Python 3.11.17)",
        "Scaffold (ubuntu-24.04, Python 3.15.0)",
        "Scaffold (macos-15, Python 3.11.17)",
    ]
    assert "permissions" not in job
    assert "continue-on-error" not in job
