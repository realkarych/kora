import io
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pytest

from scripts.ci.mutation_audit import ROOT
from scripts.ci.mutation_models import Report
from scripts.ci.mutation_process import Stop, environment, execute
from scripts.ci.mutation_remote import NativeRun, validated_state

REVISION = "a" * 40


def native_run() -> NativeRun:
    return NativeRun(
        id=10,
        run_attempt=2,
        head_sha=REVISION,
        head_branch="master",
        event="schedule",
        conclusion="success",
        path=".github/workflows/mutation.yml",
    )


@pytest.mark.parametrize(
    "changed",
    ["none", "source_revision", "tooling_revision", "run_id", "attempt", "event"],
)
def test_native_metadata_binds_continuation_state(changed: str) -> None:
    report = Report(
        source_revision=REVISION,
        tooling_revision=REVISION,
        run_id=10,
        attempt=2,
        event="schedule",
    )
    updates: dict[str, object] = {
        "source_revision": "b" * 40,
        "tooling_revision": "b" * 40,
        "run_id": 11,
        "attempt": 3,
        "event": "local",
    }
    if changed != "none":
        report = report.model_copy(update={changed: updates[changed]})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("report.json", report.model_dump_json())

    if changed == "none":
        accepted = validated_state(data.getvalue(), native_run())

        assert accepted == report
    else:
        with pytest.raises(ValueError, match="identity"):
            validated_state(data.getvalue(), native_run())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_branch", "topic"),
        ("event", "pull_request"),
        ("path", ".github/workflows/ci.yml"),
        ("conclusion", "failure"),
    ],
)
def test_untrusted_publishers_are_refused(field: str, value: str) -> None:
    run = native_run().model_copy(update={field: value})

    with pytest.raises(ValueError, match="publisher"):
        validated_state(b"unused", run)


@pytest.mark.parametrize("field", ["tooling_dirty", "retained_workspace"])
def test_unqualified_execution_state_is_refused(field: str) -> None:
    report = Report(
        source_revision=REVISION,
        tooling_revision=REVISION,
        run_id=10,
        attempt=2,
        event="schedule",
    )
    changed = report.model_copy(update={field: True})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("report.json", changed.model_dump_json())

    with pytest.raises(ValueError, match="identity"):
        validated_state(data.getvalue(), native_run())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expired", True),
        ("name", "mutation-10-1"),
        ("size_in_bytes", 0),
        ("size_in_bytes", 8 * 1024 * 1024 + 1),
    ],
)
def test_artifact_attempt_expiry_and_size_are_admission_checks(
    field: str, value: object
) -> None:
    from scripts.ci.mutation_remote import Artifact, eligible

    artifact = Artifact(id=1, name="mutation-10-2", expired=False, size_in_bytes=100)
    changed = artifact.model_copy(update={field: value})

    accepted = eligible(changed, native_run())

    assert accepted is False


def test_old_rotation_progresses_despite_continuous_new_sites() -> None:
    from scripts.ci.mutation_models import Result, ordered, rotation_order
    from tests.tooling.test_mutation_audit import mutation

    old = [mutation(0, "old-a"), mutation(1, "old-b")]
    previous = Report(
        source_revision=REVISION,
        tooling_revision=REVISION,
        rotation=tuple(item.identity() for item in old),
        results={
            item.identity(): Result(
                mutation=item,
                outcome="pending",
                input_digest="old",
                tested_revision=REVISION,
            )
            for item in old
        },
    )
    visited: list[str] = []

    for generation in range(4):
        current = [mutation(2, f"new-{generation}"), *old]
        chosen = ordered(current, previous)[0]
        visited.append(chosen.identity())
        rotation = tuple(
            item.identity()
            for item in rotation_order(current, previous)
            if item.identity() != chosen.identity()
        )
        previous = Report(
            source_revision=REVISION,
            tooling_revision=REVISION,
            generation=generation + 1,
            rotation=rotation,
            results={
                item.identity(): Result(
                    mutation=item,
                    outcome="pending",
                    input_digest="changed",
                    tested_revision=REVISION,
                )
                for item in current
            },
        )

    assert visited[1] == "old-a"
    assert visited[3] == "old-b"


@pytest.mark.parametrize("changed", ["event", "ref", "event_sha", "checkout_sha"])
def test_native_context_is_refused_before_preparation(
    tmp_path: Path, changed: str
) -> None:
    git = shutil.which("git")
    uv = shutil.which("uv")
    assert git is not None
    assert uv is not None
    revision = (
        execute([git, "rev-parse", "HEAD"], ROOT, time.monotonic() + 10, Stop())
        .output.decode()
        .strip()
    )
    variables = environment(ROOT)
    variables.update(
        {
            "PATH": str(Path(uv).parent) + os.pathsep + os.defpath,
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_REF": "refs/heads/master",
            "GITHUB_SHA": revision,
        }
    )
    if changed == "checkout_sha":
        revision = "a" * 40
        variables["GITHUB_SHA"] = revision
    elif changed == "event_sha":
        variables["GITHUB_SHA"] = "a" * 40
    elif changed == "ref":
        variables["GITHUB_REF"] = "refs/heads/topic"
    else:
        variables["GITHUB_EVENT_NAME"] = "pull_request"
    program = (
        "import time; from pathlib import Path; import pytest; "
        "from scripts.ci.mutation_audit import ROOT, audit; "
        "from scripts.ci.mutation_process import Stop\n"
        "with pytest.raises(ValueError, match='requires exact native master'):\n"
        f"    audit(ROOT, {revision!r}, Path({str(tmp_path)!r}), Stop(), time.time())\n"
    )

    refused = execute(
        [sys.executable, "-B", "-c", program],
        ROOT,
        time.monotonic() + 10,
        Stop(),
        variables=variables,
    )

    assert refused.code == 0, refused.output.decode()
    assert not list(tmp_path.iterdir())
