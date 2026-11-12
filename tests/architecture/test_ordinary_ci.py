import os
import select
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import pytest

from scripts.ci.lane_plan import MAXIMUM_LANES
from tests.measurements.storage_evidence_common import EvidenceFailure, junit_nodes
from tests.scaffold_evidence import CHILD_NAMES

ROOT = Path(__file__).resolve().parents[2]
COHORT_SCRIPT = ROOT / "scripts/ci/ordinary_cohorts.sh"
LANE_SCRIPT = ROOT / "scripts/ci/scaffold_lane.sh"


def shard_evidence(children: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"{child}.{suffix}" for child in children for suffix in ("log", "xml", "exit")
    )


def sourced(script: Path, commands: str) -> list[str]:
    return ["/bin/bash", "-c", f'source "$1"\n{commands}', "bash", str(script)]


def toml_table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def configured_testpaths() -> tuple[str, ...]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = toml_table(
        toml_table(toml_table(document["tool"])["pytest"])["ini_options"]
    )
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


LANE_NAMES = [
    ("ubuntu-24.04", "general-1", True),
    ("ubuntu-24.04", f"general-{MAXIMUM_LANES}", True),
    ("macos-15", "general-3", True),
    ("ubuntu-24.04", "storage", True),
    ("macos-15", "local-runtime", True),
    ("ubuntu-24.04", "general-0", False),
    ("ubuntu-24.04", f"general-{MAXIMUM_LANES + 1}", False),
    ("macos-15", "general-10", False),
    ("ubuntu-24.04", "conformance", False),
    ("macos-15", "remainder", False),
    ("ubuntu-24.04", "integration", False),
    ("ubuntu-24.04", "static-checks", False),
    ("macos-15", "static-checks", False),
    ("ubuntu-24.04", "", False),
    ("macos-26", "general-1", False),
]


@pytest.mark.parametrize(("system", "lane", "admitted"), LANE_NAMES)
def test_lane_shell_admits_only_planned_lane_names(
    tmp_path: Path, system: str, lane: str, admitted: bool
) -> None:
    environment = {**os.environ, "KORA_SCAFFOLD_OS": system, "KORA_SCAFFOLD_LANE": lane}

    result = subprocess.run(
        sourced(LANE_SCRIPT, "admit_lane\nprintf admitted\n"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is admitted
    assert (result.stdout == b"admitted") is admitted


HEADROOM_WARNING = b"::warning title=Scaffold lane headroom::macos-15 Python 3.11.17 "


@pytest.mark.parametrize(("elapsed", "warned"), [(400, False), (451, True)])
def test_lane_warns_when_its_job_runs_beyond_the_headroom_threshold(
    tmp_path: Path, elapsed: int, warned: bool
) -> None:
    environment = {
        **os.environ,
        "KORA_SCAFFOLD_OS": "macos-15",
        "KORA_SCAFFOLD_PYTHON": "3.11.17",
        "KORA_SCAFFOLD_LANE": "general-2",
    }
    commands = (
        f"KORA_LANE_STARTED=$(($(date +%s) - {elapsed}))\n"
        "require_lane_start\ntrap report_headroom EXIT\nexit 3\n"
    )

    result = subprocess.run(
        sourced(LANE_SCRIPT, commands),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 3
    expected = HEADROOM_WARNING + b"general-2 lane job ran"
    assert result.stdout.startswith(expected) is warned


@pytest.mark.parametrize("started", ["", "0", "1.5", "soon"])
def test_lane_refuses_a_missing_job_start(tmp_path: Path, started: str) -> None:
    environment = {**os.environ, "KORA_LANE_STARTED": started}

    result = subprocess.run(
        sourced(LANE_SCRIPT, "require_lane_start\nprintf started\n"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode != 0
    assert result.stdout == b""


def test_children_run_exactly_their_argument_files(tmp_path: Path) -> None:
    source = tmp_path / "test_planned.py"
    source.write_text(
        "def test_first() -> None:\n    pass\n\n\n"
        "def test_second() -> None:\n    pass\n\n\n"
        "def test_third() -> None:\n    pass\n"
    )
    output = tmp_path / "output"
    output.mkdir()
    root = f"--rootdir={tmp_path}\n"
    (output / "child-1.args").write_text(
        f"{root}{source}::test_third\n{source}::test_first\n"
    )
    (output / "child-2.args").write_text(f"{root}{source}::test_second\n")

    result = subprocess.run(
        ["/bin/bash", str(COHORT_SCRIPT)],
        cwd=ROOT,
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(output)},
        capture_output=True,
        check=False,
        timeout=60,
    )

    first_log = (output / "child-1.log").read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert junit_nodes(output / "child-1.xml") == {
        "test_planned.test_third",
        "test_planned.test_first",
    }
    assert junit_nodes(output / "child-2.xml") == {"test_planned.test_second"}
    assert first_log.index("test_third") < first_log.index("test_first")
    assert [(output / f"child-{index}.exit").read_text() for index in (1, 2)] == [
        "0\n",
        "0\n",
    ]


@pytest.mark.parametrize("invalid", ["empty", "symlink", "missing"])
def test_launcher_refuses_invalid_argument_files_before_any_child_starts(
    tmp_path: Path, invalid: str
) -> None:
    first = tmp_path / "child-1.args"
    second = tmp_path / "child-2.args"
    first.write_text("tests/test_value.py::test_value\n")
    if invalid == "empty":
        second.write_text("")
    elif invalid == "symlink":
        second.symlink_to(first)
    else:
        first.unlink()

    result = subprocess.run(
        sourced(COHORT_SCRIPT, "launch_children\nprintf launched\n"),
        cwd=tmp_path,
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert not any(tmp_path.glob("*.log"))


@pytest.mark.parametrize("failed_child", [None, 0, 1, 2])
def test_ordinary_shell_joins_all_owned_children_after_failure(
    tmp_path: Path, failed_child: int | None
) -> None:
    launches = ["pids=()", "names=(child-1 child-2 child-3)"]
    for index in range(3):
        exit_code = 1 if index == failed_child else 0
        launches.append(f'(printf complete > "{index}"; exit {exit_code}) &')
        launches.append('pids+=("$!")')
    script = "\n".join(launches) + '\njoin_groups\nexit "$result"\n'

    result = subprocess.run(
        sourced(COHORT_SCRIPT, script),
        cwd=tmp_path,
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == (0 if failed_child is None else 1)
    assert all((tmp_path / str(index)).read_text() == "complete" for index in range(3))
    for index, name in enumerate(CHILD_NAMES):
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


@pytest.mark.parametrize("fails", [False, True])
def test_ordinary_first_failure_finalizes_fixture_and_evidence(
    tmp_path: Path, fails: bool
) -> None:
    source = tmp_path / "test_exitfirst.py"
    report = tmp_path / "junit.xml"
    source.write_text(
        "from collections.abc import Iterator\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        f"FAILS = {fails!r}\n\n"
        '@pytest.fixture(scope="session")\n'
        "def owned_resource() -> Iterator[Path]:\n"
        '    resource = Path(__file__).with_name("owned-resource")\n'
        "    resource.mkdir()\n"
        "    try:\n"
        "        yield resource\n"
        "    finally:\n"
        "        resource.rmdir()\n"
        '        resource.with_name("released").write_text("released")\n\n'
        "def test_first(owned_resource: Path) -> None:\n"
        "    assert owned_resource.is_dir()\n"
        '    assert not FAILS, "first failure evidence"\n\n'
        "def test_later(owned_resource: Path) -> None:\n"
        "    assert owned_resource.is_dir()\n"
        '    owned_resource.with_name("later-ran").write_text("ran")\n'
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.ordinary_pytest",
            "-v",
            "--exitfirst",
            f"--rootdir={tmp_path}",
            f"--junitxml={report}",
            str(source),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == int(fails), result.stdout + result.stderr
    assert not (tmp_path / "owned-resource").exists()
    assert (tmp_path / "released").read_text() == "released"
    assert (tmp_path / "later-ran").exists() is (not fails)
    if fails:
        assert b"AssertionError: first failure evidence" in result.stdout
        assert b"1 failed" in result.stdout
        with pytest.raises(EvidenceFailure, match="test did not pass"):
            junit_nodes(report)
    else:
        assert b"2 passed" in result.stdout
        assert junit_nodes(report) == frozenset(
            {"test_exitfirst.test_first", "test_exitfirst.test_later"}
        )


def test_interrupted_cohort_preserves_only_regular_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    environment_file = tmp_path / "environment"
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(environment_file),
        "KORA_SCAFFOLD_DIRECTORY": str(evidence),
    }
    script = (
        "open_ordinary_output\n"
        'printf entered > "$KORA_ORDINARY_OUTPUT/child-1.log"\n'
        "printf ready\\n\nread -r finish\n"
    )

    with subprocess.Popen(
        sourced(LANE_SCRIPT, script),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 5)
            assert readable
            assert process.stdout.read(5) == b"ready"
            output = Path(environment_file.read_text().strip().partition("=")[2])
            assert (output / "child-1.log").read_text() == "entered"
            process.terminate()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    (output / "cache-child-2").mkdir()
    (output / "tmp-child-2").mkdir()
    (output / "tmp-child-2" / "private").write_text("fixture-payload")
    (output / "child-2.log").symlink_to(output / "tmp-child-2" / "private")
    os.mkfifo(output / "child-2.xml")
    (output / "child-3.xml").mkdir()
    (output / "child-3.args").write_text("tests/test_value.py::test_value\n")
    copied = subprocess.run(
        ["/bin/bash", str(LANE_SCRIPT), "preserve"],
        env={**environment, "KORA_ORDINARY_OUTPUT": str(output)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert process.returncode is not None
    assert process.returncode != 0
    assert copied.returncode == 0, copied.stderr
    assert sorted(path.name for path in evidence.iterdir()) == ["child-1.log"]
    assert (evidence / "child-1.log").read_text() == "entered"
    assert not (evidence / "receipt.json").exists()


@pytest.mark.parametrize("invalid", [None, "missing", "empty", "symlink"])
def test_ordinary_shell_requires_all_regular_nonempty_evidence(
    tmp_path: Path, invalid: str | None
) -> None:
    evidence = shard_evidence(CHILD_NAMES)
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
    assignment = " ".join(evidence)

    result = subprocess.run(
        sourced(COHORT_SCRIPT, f"evidence=({assignment})\nrequire_group_evidence\n"),
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (invalid is None)
