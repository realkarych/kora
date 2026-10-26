import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.architecture.test_measurement_job_routing import steps, table, workflow_jobs

FIRST_MODULES = (
    "tests/conformance/test_conformance_cases_detect_broken_providers.py",
    "tests/conformance/test_lifetime_helper_phases.py",
    "tests/conformance/test_lifetime_measurement_cohorts.py",
    "tests/conformance/test_lifetime_parent_exit.py",
    "tests/conformance/test_lifetime_pending_helpers.py",
    "tests/conformance/test_lifetime_resume.py",
    "tests/conformance/test_lifetime_rpc_concurrency.py",
    "tests/conformance/test_lifetime_services.py",
    "tests/conformance/test_local_process_lifetime.py",
    "tests/conformance/test_parent_lifetime.py",
    "tests/conformance/test_recovered_process_lifetime.py",
)
EVIDENCE_FILES = (
    "full.collection",
    "first.collection",
    "second.collection",
    "other.collection",
    "inventory.json",
    "first.log",
    "second.log",
    "other.log",
    "first.xml",
    "second.xml",
    "other.xml",
    "first.exit",
    "second.exit",
    "other.exit",
)


def ordinary_shell() -> str:
    source = str(steps(table(workflow_jobs()["scaffold-tests"]))["tests"]["run"])
    return (
        source.partition("KORA_ORDINARY_SCRIPT' >")[2]
        .partition("\n")[2]
        .partition("KORA_ORDINARY_SCRIPT\n")[0]
    )


def test_ordinary_ci_collects_exact_groups_and_uploads_only_evidence() -> None:
    selected = steps(table(workflow_jobs()["scaffold-tests"]))
    command = ordinary_shell()

    first = command.partition("first=(\n")[2].partition(")\n")[0].split()

    assert tuple(first) == FIRST_MODULES
    assert (
        "second=(tests/conformance "
        "--ignore=tests/conformance/test_artifact_storage_conformance.py "
        "tests/measurements/test_ir_measurement_evidence.py)\n"
        'for path in "${first[@]}"; do\n'
        '  second+=("--ignore=$path")\ndone\n'
        "other=(tests --ignore=tests/conformance --ignore=tests/plugins/storage "
        "--ignore=tests/measurements/test_ir_measurement_evidence.py)\n"
    ) in command
    assert 'mktemp -d "$RUNNER_TEMP/kora-ordinary.XXXXXX"' in str(
        selected["tests"]["run"]
    )
    for group, arguments in (
        ("full", '"${general[@]}"'),
        ("first", '"${first[@]}"'),
        ("second", '"${second[@]}"'),
        ("other", '"${other[@]}"'),
    ):
        assert (
            "uv run --no-sync pytest --collect-only -q -o "
            f'cache_dir="$KORA_ORDINARY_OUTPUT/cache-{group}" {arguments} '
            f'> "$KORA_ORDINARY_OUTPUT/{group}.collection"\n'
        ) in command
        if group != "full":
            assert (
                "uv run --no-sync python -m tests.support.ordinary_pytest -q -o "
                f'cache_dir="$KORA_ORDINARY_OUTPUT/cache-{group}" '
                f'--basetemp="$KORA_ORDINARY_OUTPUT/tmp-{group}" '
                f'--junitxml="$KORA_ORDINARY_OUTPUT/{group}.xml" {arguments} '
                f'> "$KORA_ORDINARY_OUTPUT/{group}.log" 2>&1 &\n'
                'pids+=("$!")\n'
            ) in command
    assert command.count('pids+=("$!")') == 3
    assert (
        command.index("other.collection")
        < command.index("python -m tests.support.ordinary_inventory")
        < command.index("pids=()")
    )
    assert (
        '  if wait "${pids[$index]}"; then\n    status=0\n  else\n'
        '    status="$?"\n    result=1\n  fi'
    ) in command
    assert (
        'printf \'%s\\n\' "$status" > "$KORA_ORDINARY_OUTPUT/${names[$index]}.exit"'
    ) in command
    assert command.index('pids+=("$!")', command.index("tmp-other")) < command.index(
        'for index in "${!pids[@]}"'
    )
    assert command.index('for index in "${!pids[@]}"') < command.index("for name in ")
    assert (
        command.index("for name in ")
        < command.index("cat ")
        < command.index('exit "$result"')
    )
    assert "for name in " + " ".join(EVIDENCE_FILES) + "; do\n" in command
    assert (
        '  test -f "$KORA_ORDINARY_OUTPUT/$name"\n'
        '  test ! -L "$KORA_ORDINARY_OUTPUT/$name"\n'
        '  test -s "$KORA_ORDINARY_OUTPUT/$name"\n'
    ) in command
    assert set(selected["tests"]) == {"name", "id", "run"}
    upload = selected["evidence_upload"]
    assert upload["if"] == "${{ always() }}"
    assert upload["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )
    assert table(upload["with"]) == {
        "name": (
            "scaffold-tests-${{ matrix.os }}-${{ matrix.python }}-${{ matrix.lane }}"
        ),
        "path": "${{ env.KORA_SCAFFOLD_DIRECTORY }}",
        "if-no-files-found": "error",
    }
    source = str(selected["tests"]["run"])
    assert 'cp "$KORA_ORDINARY_OUTPUT/$name" "$KORA_SCAFFOLD_DIRECTORY/$name"' in source
    assert source.index("KORA_ORDINARY_SCRIPT\n") < source.index("cp ")
    assert source.index("cp ") < source.index(
        "python -m tests.scaffold_evidence record"
    )
    assert "ordinary_tests" not in selected
    assert "ordinary_upload" not in selected


@pytest.mark.parametrize("failed_child", [None, 0, 1, 2])
def test_ordinary_shell_joins_all_owned_children_after_failure(
    tmp_path: Path, failed_child: int | None
) -> None:
    join = ordinary_shell().partition("result=0\n")[2].partition("for name in ")[0]
    launches = ["pids=()"]
    for index in range(3):
        exit_code = 1 if index == failed_child else 0
        launches.append(f'(printf complete > "{index}"; exit {exit_code}) &')
        launches.append('pids+=("$!")')
    script = "\n".join(launches) + "\nresult=0\n" + join + 'exit "$result"\n'

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == (0 if failed_child is None else 1)
    assert all((tmp_path / str(index)).read_text() == "complete" for index in range(3))
    for index, name in enumerate(("first", "second", "other")):
        assert (tmp_path / f"{name}.exit").read_text() == (
            "1\n" if index == failed_child else "0\n"
        )


def test_ordinary_background_pytest_preserves_interruptible_children(
    tmp_path: Path,
) -> None:
    source = tmp_path / "test_signal.py"
    source.write_text(
        "import signal\n\n"
        "def test_interruptible_child() -> None:\n"
        "    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
    )
    script = (
        '"$KORA_TEST_PYTHON" -m tests.support.ordinary_pytest -q '
        '"$KORA_TEST_SOURCE" &\n'
        'pid="$!"\nwait "$pid"\n'
    )

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "KORA_TEST_PYTHON": sys.executable,
            "KORA_TEST_SOURCE": str(source),
        },
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert b"1 passed" in result.stdout


@pytest.mark.parametrize("invalid", [None, "missing", "empty", "symlink"])
def test_ordinary_shell_requires_all_regular_nonempty_evidence(
    tmp_path: Path, invalid: str | None
) -> None:
    for name in EVIDENCE_FILES:
        (tmp_path / name).write_text("complete")
    target = tmp_path / "second.xml"
    if invalid == "missing":
        target.unlink()
    elif invalid == "empty":
        target.write_text("")
    elif invalid == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "first.xml")
    validation = "for name in " + ordinary_shell().partition("for name in ")[2]
    validation = validation.partition("cat ")[0]

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", validation],
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (invalid is None)


@pytest.mark.parametrize("field", ["TESTS_OUTCOME", "UPLOAD_OUTCOME"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped", ""])
def test_ordinary_admission_rejects_every_incomplete_outcome(
    field: str, outcome: str
) -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    entries = cast(list[object], job["steps"])
    gate = table(entries[-1])
    environment = {
        **os.environ,
        **dict.fromkeys(
            (
                "DEPENDENCIES_OUTCOME",
                "FRONTEND_OUTCOME",
                "STATIC_OUTCOME",
                "TESTS_OUTCOME",
                "UPLOAD_OUTCOME",
                "DISTRIBUTIONS_OUTCOME",
                "FINAL_SYNC_OUTCOME",
            ),
            "success",
        ),
        "WORKFLOW_CANCELLED": "false",
        field: outcome,
    }

    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            str(gate["run"]).replace("${{ matrix.lane }}", "general"),
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (outcome == "success")
