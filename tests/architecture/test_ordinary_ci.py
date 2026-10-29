import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import pytest

from tests.scaffold_evidence import COHORTS, SHARD_CHILDREN
from tests.support.workflows import (
    command_lines,
    steps,
    table,
    workflow_jobs,
)

ROOT = Path(__file__).resolve().parents[2]
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
COMPLETION_FIELDS = (
    "DEPENDENCIES_OUTCOME",
    "PREPARATION_OUTCOME",
    "TEARDOWN_OUTCOME",
    "FRONTEND_OUTCOME",
    "STATIC_OUTCOME",
    "TESTS_OUTCOME",
    "UPLOAD_OUTCOME",
    "DISTRIBUTIONS_OUTCOME",
    "FINAL_SYNC_OUTCOME",
)
WITHOUT_FIXTURES = {"PREPARATION_OUTCOME": "skipped", "TEARDOWN_OUTCOME": "skipped"}
WITHOUT_BUILDS = {"STATIC_OUTCOME": "skipped", "DISTRIBUTIONS_OUTCOME": "skipped"}
LANE_OUTCOMES = {
    "conformance": WITHOUT_FIXTURES,
    "remainder": {**WITHOUT_FIXTURES, **WITHOUT_BUILDS},
    "storage": {**WITHOUT_FIXTURES, **WITHOUT_BUILDS},
    "local-runtime": {**WITHOUT_BUILDS, "FRONTEND_OUTCOME": "skipped"},
}


def shard_evidence(shard: str) -> tuple[str, ...]:
    children = SHARD_CHILDREN[shard]
    return tuple(
        f"{child}.{suffix}" for child in children for suffix in ("log", "xml", "exit")
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


ORDINARY_OUTPUT = "$KORA_ORDINARY_OUTPUT"
BASE_TEMPORARY = "tmp-"
PID_RECORD = ("pids+=", "(", "$!", ")")


def launch_command(child: str) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "tests.support.ordinary_pytest",
        "-q",
        "-o",
        f"cache_dir={ORDINARY_OUTPUT}/cache-{child}",
        f"--basetemp={ORDINARY_OUTPUT}/{BASE_TEMPORARY}{child}",
        f"--junitxml={ORDINARY_OUTPUT}/{child}.xml",
        f"${{{child}[@]}}",
        ">",
        f"{ORDINARY_OUTPUT}/{child}.log",
        "2",
        ">&",
        "1",
        "&",
    )


def declared_array(lines: list[tuple[str, ...]], name: str) -> list[str]:
    values = [
        list(line[2:-1])
        for line in lines
        if line[:2] == (f"{name}=", "(") and line[-1] == ")"
    ]
    assert len(values) == 1
    return values[0]


def rendered(template: str, row: dict[str, object]) -> str:
    for key, value in row.items():
        template = template.replace(f"${{{{ matrix.{key} }}}}", str(value))
    return template


def test_ordinary_ci_collects_exact_groups_and_uploads_only_evidence() -> None:
    jobs = workflow_jobs()
    lane = table(jobs["scaffold-tests"])
    selected = steps(lane)
    command = ordinary_shell()
    lines = command_lines(command)
    outer = command_lines(selected["tests"]["run"])
    collection = [line for line in outer if "--collect-only" in line]
    download = table(
        steps(table(jobs["scaffold-evidence"]))["evidence_download"]["with"]
    )

    assert "${{" not in command
    assert len(collection) == 1
    assert collection[0][:8] == (
        "if",
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "--collect-only",
        "-q",
        "-m",
    )
    assert "not measurement" in collection[0]
    assert not any("--collect-only" in line for line in lines)
    assert download["pattern"] == "scaffold-tests-*"
    for shard, children in SHARD_CHILDREN.items():
        branch = command_lines(shard_branch(shard))
        names = declared_array(branch, "names")
        launches = [launch_command(child) for child in names]
        assert sorted(names) == sorted(children)
        assert [
            line for line in branch if "tests.support.ordinary_pytest" in line
        ] == launches
        assert all(
            branch[branch.index(launch) + 1] == PID_RECORD for launch in launches
        )
        assert branch.count(PID_RECORD) == len(children)
        assert sorted(declared_array(branch, "evidence")) == sorted(
            shard_evidence(shard)
        )
    assert (
        lines.index(("result=0",))
        < lines.index(("for", "index", "in", "${!pids[@]}", ";", "do"))
        < lines.index(("for", "name", "in", "${evidence[@]}", ";", "do"))
    )
    assert lines[-1] == ("exit", "$result")
    assert selected["evidence_upload"]["if"] == "${{ always() }}"
    assert (
        table(selected["evidence_upload"]["with"])["path"]
        == "${{ env.KORA_SCAFFOLD_DIRECTORY }}"
    )
    assert "tests.scaffold_evidence" in str(selected["tests"]["run"])
    assert "record" in str(selected["tests"]["run"])


def configured_testpaths() -> tuple[str, ...]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = table(table(table(document["tool"])["pytest"])["ini_options"])
    return tuple(str(path) for path in cast(list[object], options["testpaths"]))


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def selected_files(arguments: list[str]) -> frozenset[str]:
    candidates = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("test_*.py")
    ]
    ignored = [
        argument.removeprefix("--ignore=")
        for argument in arguments
        if argument.startswith("--ignore=")
    ]
    roots = [argument for argument in arguments if not argument.startswith("-")]
    return frozenset(
        path
        for path in candidates
        if any(is_under(path, root) for root in roots or configured_testpaths())
        and not any(is_under(path, ignore) for ignore in ignored)
    )


def test_ordinary_cohorts_partition_the_general_lane_files(tmp_path: Path) -> None:
    lane = table(workflow_jobs()["scaffold-tests"])
    row = next(
        table(value)
        for value in cast(
            list[object], table(table(lane["strategy"])["matrix"])["include"]
        )
        if table(value)["lane"] == "conformance"
    )
    declarations = ordinary_shell().partition("\npids=()")[0]
    printing = "".join(
        f'printf "%s\\0" "${{{name}[@]}}" > "$KORA_COHORT_OUTPUT/{name}"\n'
        for name in ("general", *COHORTS)
    )

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", f"{declarations}\n{printing}"],
        env={
            **os.environ,
            "KORA_SCAFFOLD_LANE": "conformance",
            "KORA_PYTEST_ARGUMENTS": str(row["pytest-arguments"]),
            "KORA_COHORT_OUTPUT": str(tmp_path),
        },
        capture_output=True,
        check=False,
        timeout=5,
    )
    arrays = {
        name: (tmp_path / name).read_text(encoding="utf-8").split("\0")[:-1]
        for name in ("general", *COHORTS)
    }
    cohorts = {cohort: selected_files(arrays[cohort]) for cohort in COHORTS}
    general = selected_files(arrays["general"])

    assert result.returncode == 0, result.stderr
    assert tuple(arrays["first"]) == FIRST_MODULES
    assert all(
        not argument.startswith("-") or argument.startswith("--ignore=")
        for values in arrays.values()
        for argument in values
    )
    assert all(
        (ROOT / argument).exists()
        for values in arrays.values()
        for argument in values
        if not argument.startswith("-")
    )
    assert all(cohorts.values())
    assert sum(len(files) for files in cohorts.values()) == len(general)
    assert frozenset[str]().union(*cohorts.values()) == general


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


LaneCompletionCase = tuple[str, dict[str, str], bool]


def lane_completion_cases() -> dict[str, LaneCompletionCase]:
    cases: dict[str, LaneCompletionCase] = {}
    for lane in LANE_OUTCOMES:
        complete = expected_outcomes(lane)
        cases[f"{lane}-complete"] = (lane, complete, True)
        cases[f"{lane}-workflow-cancelled"] = (
            lane,
            {**complete, "WORKFLOW_CANCELLED": "true"},
            False,
        )
        for field, expected in complete.items():
            for outcome in ("success", "failure", "cancelled", "skipped", ""):
                if outcome != expected:
                    cases[f"{lane}-{field}-{outcome or 'empty'}"] = (
                        lane,
                        {**complete, field: outcome},
                        False,
                    )
    cases["unknown-conformance-profile"] = (
        "unknown",
        expected_outcomes("conformance"),
        False,
    )
    cases["unknown-reduced-profile"] = (
        "unknown",
        expected_outcomes("remainder"),
        False,
    )
    cases["empty-conformance-profile"] = ("", expected_outcomes("conformance"), False)
    return cases


LANE_COMPLETION_CASES = lane_completion_cases()


def test_lane_completion_gate_always_runs_without_workflow_expressions() -> None:
    job = table(workflow_jobs()["scaffold-tests"])

    gate = table(cast(list[object], job["steps"])[-1])

    assert gate["if"] == "${{ always() }}"
    assert "${{" not in str(gate["run"])


@pytest.mark.parametrize(
    ("lane", "outcomes", "passes"),
    list(LANE_COMPLETION_CASES.values()),
    ids=list(LANE_COMPLETION_CASES),
)
def test_lane_completion_rejects_every_incomplete_outcome(
    lane: str, outcomes: dict[str, str], passes: bool
) -> None:
    job = table(workflow_jobs()["scaffold-tests"])
    gate = table(cast(list[object], job["steps"])[-1])
    environment = {
        **os.environ,
        "WORKFLOW_CANCELLED": "false",
        **outcomes,
        "KORA_SCAFFOLD_LANE": lane,
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate["run"])],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is passes
