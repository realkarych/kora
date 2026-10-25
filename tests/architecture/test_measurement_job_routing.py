import os
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


def table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


def workflow_jobs() -> dict[str, object]:
    document = cast(YamlLoader, YAML(typ="safe")).load(
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    )
    return table(table(document)["jobs"])


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
        for operating_system, python, databases in (
            ("ubuntu-24.04", "3.11.17", "sqlite,postgresql"),
            ("ubuntu-24.04", "3.15.0", "sqlite,postgresql"),
            ("macos-15", "3.11.17", "sqlite"),
        )
        for lane, arguments in (
            (
                "general",
                "--ignore=tests/plugins/storage "
                "--ignore=tests/conformance/test_artifact_storage_conformance.py",
            ),
            (
                "storage",
                "tests/plugins/storage "
                "tests/conformance/test_artifact_storage_conformance.py",
            ),
        )
    ]
    assert "uv run --no-sync pytest -q" in commands
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
    ["resource-measurements", "pricing-measurements", "git-publication-measurements"],
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
