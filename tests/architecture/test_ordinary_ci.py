import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.architecture.test_measurement_job_routing import steps, table, workflow_jobs
from tests.scaffold_evidence import COHORTS, SHARD_CHILDREN

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
COLLECTIONS = (
    "full.collection",
    *(f"{cohort}.collection" for cohort in COHORTS),
)
GROUP_ARGUMENTS = {
    "full": '"${general[@]}"',
    **{cohort: f'"${{{cohort}[@]}}"' for cohort in COHORTS},
}
COMPLETION_FIELDS = (
    "DEPENDENCIES_OUTCOME",
    "FRONTEND_OUTCOME",
    "STATIC_OUTCOME",
    "TESTS_OUTCOME",
    "UPLOAD_OUTCOME",
    "DISTRIBUTIONS_OUTCOME",
    "FINAL_SYNC_OUTCOME",
)
LANE_OUTCOMES = {
    "conformance": {"STATIC_OUTCOME": "skipped", "DISTRIBUTIONS_OUTCOME": "skipped"},
    "remainder": {},
    "storage": {
        "FRONTEND_OUTCOME": "skipped",
        "STATIC_OUTCOME": "skipped",
        "DISTRIBUTIONS_OUTCOME": "skipped",
    },
}


def shard_evidence(shard: str) -> tuple[str, ...]:
    children = SHARD_CHILDREN[shard]
    return (
        *COLLECTIONS,
        "inventory.json",
        *(f"{child}.log" for child in children),
        *(f"{child}.xml" for child in children),
        *(f"{child}.exit" for child in children),
    )


def expected_outcomes(lane: str) -> dict[str, str]:
    return {**dict.fromkeys(COMPLETION_FIELDS, "success"), **LANE_OUTCOMES[lane]}


def ordinary_shell() -> str:
    source = str(steps(table(workflow_jobs()["scaffold-tests"]))["tests"]["run"])
    return (
        source.partition("KORA_ORDINARY_SCRIPT' >")[2]
        .partition("\n")[2]
        .partition("KORA_ORDINARY_SCRIPT\n")[0]
    )


def shard_branch(shard: str) -> str:
    launches = ordinary_shell().partition('pids=()\ncase "$KORA_SCAFFOLD_LANE" in\n')[2]
    return launches.partition(f"  {shard})\n")[2].partition("    ;;\n")[0]


def evidence_validation() -> str:
    marker = 'for name in "${evidence[@]}"; do\n  test -f'
    remainder = ordinary_shell().partition(marker)[2]
    return marker + remainder.partition("done\n")[0] + "done\n"


def test_ordinary_ci_collects_exact_groups_and_uploads_only_evidence() -> None:
    selected = steps(table(workflow_jobs()["scaffold-tests"]))
    command = ordinary_shell()
    source = str(selected["tests"]["run"])

    first = command.partition("first=(\n")[2].partition(")\n")[0].split()

    assert command.startswith(
        'read -r -a general <<< "$KORA_PYTEST_ARGUMENTS"\n'
        'case "$KORA_SCAFFOLD_LANE" in\n'
        "  conformance|remainder) ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    assert "${{" not in command
    assert tuple(first) == FIRST_MODULES
    assert (
        "second=(tests/conformance "
        "--ignore=tests/conformance/test_artifact_storage_conformance.py)\n"
        'for path in "${first[@]}"; do\n'
        '  second+=("--ignore=$path")\ndone\n'
        "measurements=(tests/measurements)\n"
        "integration=(tests/db tests/cli tests/plugins "
        "--ignore=tests/plugins/storage)\n"
        "other=(tests --ignore=tests/conformance --ignore=tests/plugins "
        "--ignore=tests/cli --ignore=tests/db --ignore=tests/measurements)\n"
    ) in command
    assert 'mktemp -d "$RUNNER_TEMP/kora-ordinary.XXXXXX"' in source
    collections = [
        command.index(
            "uv run --no-sync pytest --collect-only -q -o "
            f'cache_dir="$KORA_ORDINARY_OUTPUT/cache-{group}" {arguments} '
            f'> "$KORA_ORDINARY_OUTPUT/{group}.collection"\n'
        )
        for group, arguments in GROUP_ARGUMENTS.items()
    ]
    assert collections == sorted(collections)
    assert (
        collections[-1]
        < command.index("python -m tests.support.ordinary_inventory")
        < command.index("pids=()")
    )
    for shard, children in SHARD_CHILDREN.items():
        launches = "".join(
            "    uv run --no-sync python -m tests.support.ordinary_pytest -q -o "
            f'cache_dir="$KORA_ORDINARY_OUTPUT/cache-{child}" '
            f'--basetemp="$KORA_ORDINARY_OUTPUT/tmp-{child}" '
            f'--junitxml="$KORA_ORDINARY_OUTPUT/{child}.xml" '
            f"{GROUP_ARGUMENTS[child]} "
            f'> "$KORA_ORDINARY_OUTPUT/{child}.log" 2>&1 &\n'
            '    pids+=("$!")\n'
            for child in children
        )
        assert shard_branch(shard) == (
            launches
            + "    names=("
            + " ".join(children)
            + ")\n    evidence=("
            + " ".join(shard_evidence(shard))
            + ")\n"
        )
    assert command.count('pids+=("$!")') == 5
    assert "    ;;\n  *) exit 1 ;;\nesac\nresult=0\n" in command
    assert (
        '  if wait "${pids[$index]}"; then\n    status=0\n  else\n'
        '    status="$?"\n    result=1\n  fi'
    ) in command
    assert (
        'printf \'%s\\n\' "$status" > "$KORA_ORDINARY_OUTPUT/${names[$index]}.exit"'
    ) in command
    assert (
        command.index("esac\nresult=0\n")
        < command.index('for index in "${!pids[@]}"')
        < command.index('for name in "${evidence[@]}"')
        < command.index('exit "$result"')
    )
    assert (
        'for name in "${evidence[@]}"; do\n'
        '  test -f "$KORA_ORDINARY_OUTPUT/$name"\n'
        '  test ! -L "$KORA_ORDINARY_OUTPUT/$name"\n'
        '  test -s "$KORA_ORDINARY_OUTPUT/$name"\n'
        "done\n"
        'for name in "${evidence[@]}"; do\n'
        '  case "$name" in\n'
        '    inventory.json|*.log) cat "$KORA_ORDINARY_OUTPUT/$name" ;;\n'
        "  esac\n"
        "done\n"
        'exit "$result"\n'
    ) in command
    copied = sorted(
        {name for shard in SHARD_CHILDREN for name in shard_evidence(shard)}
    )
    copy_list = source.partition("KORA_ORDINARY_SCRIPT\n")[2]
    copy_list = copy_list.partition("for name in ")[2].partition("; do\n")[0]
    assert sorted(copy_list.split()) == copied
    assert 'cp "$KORA_ORDINARY_OUTPUT/$name" "$KORA_SCAFFOLD_DIRECTORY/$name"' in source
    assert source.index("KORA_ORDINARY_SCRIPT\n") < source.index("cp ")
    assert source.index("cp ") < source.index(
        "python -m tests.scaffold_evidence record"
    )
    assert 'if test "$KORA_SCAFFOLD_LANE" = conformance; then\n' in source
    assert 'if test "$KORA_SCAFFOLD_LANE" != storage; then\n' in source
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
    assert "ordinary_tests" not in selected
    assert "ordinary_upload" not in selected


def test_shards_partition_every_inventory_group() -> None:
    children = [child for shard in SHARD_CHILDREN.values() for child in shard]

    assert sorted(children) == sorted(COHORTS)
    assert len(children) == len(set(children))


@pytest.mark.parametrize("lane", ["conformance", "remainder", "", "storage", "all"])
def test_ordinary_shell_refuses_unknown_shard_before_collection(
    tmp_path: Path, lane: str
) -> None:
    opening = ordinary_shell().partition("esac\n")[0] + "esac\n"

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", opening + "printf started\n"],
        cwd=tmp_path,
        env={**os.environ, "KORA_SCAFFOLD_LANE": lane, "KORA_PYTEST_ARGUMENTS": ""},
        capture_output=True,
        check=False,
        timeout=5,
    )

    valid = lane in SHARD_CHILDREN
    assert result.returncode == (0 if valid else 1)
    assert (result.stdout == b"started") is valid


@pytest.mark.parametrize("failed_child", [None, 0, 1, 2])
def test_ordinary_shell_joins_all_owned_children_after_failure(
    tmp_path: Path, failed_child: int | None
) -> None:
    join = ordinary_shell().partition("result=0\n")[2].partition("for name in ")[0]
    launches = ["pids=()", "names=(measurements integration other)"]
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
    for index, name in enumerate(("measurements", "integration", "other")):
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


@pytest.mark.parametrize("shard", list(SHARD_CHILDREN))
@pytest.mark.parametrize("invalid", [None, "missing", "empty", "symlink"])
def test_ordinary_shell_requires_all_regular_nonempty_evidence(
    tmp_path: Path, shard: str, invalid: str | None
) -> None:
    evidence = shard_evidence(shard)
    for name in evidence:
        (tmp_path / name).write_text("complete")
    target = tmp_path / evidence[-1]
    if invalid == "missing":
        target.unlink()
    elif invalid == "empty":
        target.write_text("")
    elif invalid == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "inventory.json")
    assignment = shard_branch(shard).rpartition("    evidence=")[2]

    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            "evidence=" + assignment + evidence_validation(),
        ],
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (invalid is None)


@pytest.mark.parametrize("lane", [*LANE_OUTCOMES, "unknown"])
@pytest.mark.parametrize("field", [None, *COMPLETION_FIELDS])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped", ""])
def test_lane_completion_rejects_every_incomplete_outcome(
    lane: str, field: str | None, outcome: str
) -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    gate = table(cast(list[object], job["steps"])[-1])
    expected = expected_outcomes(lane if lane in LANE_OUTCOMES else "conformance")
    environment = {
        **os.environ,
        **expected,
        "WORKFLOW_CANCELLED": "false",
        "KORA_SCAFFOLD_LANE": lane,
    }
    if field is not None:
        environment[field] = outcome

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert gate["if"] == "${{ always() }}"
    assert "${{" not in str(gate["run"])
    assert (result.returncode == 0) is (
        lane in LANE_OUTCOMES and (field is None or outcome == expected[field])
    )
