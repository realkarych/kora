import io
import os
import selectors
import shutil
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.ci.mutation_audit import (
    ROOT,
    classify,
    clean_bytecode,
    restored_source,
    save,
    unpack,
    workspace,
)
from scripts.ci.mutation_models import Mutation, Report, Result, Scope, digest, ordered
from scripts.ci.mutation_process import (
    ClosureFailure,
    Execution,
    Stop,
    environment,
    execute,
)

REVISION = "a" * 40


def mutation(occurrence: int, fingerprint: str = "") -> Mutation:
    return Mutation(
        module_path="src/kora/ir/pricing.py",
        operator_name="core/NumberReplacer",
        occurrence=occurrence,
        start_pos=(1, 0),
        end_pos=(1, 1),
        definition_name="estimate_usage_usd",
        fingerprint=fingerprint,
    )


def runner_arguments(root: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-q",
        "-x",
        "--tb=no",
        f"--junitxml={root / 'results.xml'}",
        str(root / "test_sample.py"),
    ]


def test_real_baseline_and_same_size_control_mutation(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def value() -> int:\n    return 2\n")
    (tmp_path / "test_sample.py").write_text(
        "from sample import value\n\ndef test_value() -> None:\n"
        "    assert value() == 2\n"
    )
    arguments = runner_arguments(tmp_path)

    original = execute(arguments, tmp_path, time.monotonic() + 10, Stop())
    original_outcome = classify(original, tmp_path / "results.xml")
    source.write_text("def value() -> int:\n    return 3\n")
    changed = execute(arguments, tmp_path, time.monotonic() + 10, Stop())
    changed_outcome = classify(changed, tmp_path / "results.xml")

    assert original_outcome == "survived"
    assert changed_outcome == "killed"
    assert not list(tmp_path.rglob("*.pyc"))


@pytest.mark.parametrize("case", ["setup", "collection", "empty"])
def test_real_pytest_errors_are_not_behavioral_kills(tmp_path: Path, case: str) -> None:
    sources = {
        "setup": (
            "import pytest\n@pytest.fixture\ndef broken() -> None:\n"
            "    raise ValueError('fixture failed')\n"
            "def test_value(broken: None) -> None:\n    assert broken is None\n"
        ),
        "collection": "from absent_test_module import value\n",
        "empty": "value = 2\n",
    }
    (tmp_path / "test_sample.py").write_text(sources[case])

    result = execute(
        runner_arguments(tmp_path), tmp_path, time.monotonic() + 10, Stop()
    )

    assert classify(result, tmp_path / "results.xml") == "test_error"


@pytest.mark.parametrize(
    "content", [b"", b"<testsuites/>", b"<!DOCTYPE x><testsuites/>"]
)
def test_junit_requires_real_nonempty_results(tmp_path: Path, content: bytes) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(content)

    outcome = classify(Execution(0, b"", "completed"), report)

    assert outcome == "test_error"


def test_timeout_and_capture_overflow_are_distinct(tmp_path: Path) -> None:
    blocked = [sys.executable, "-c", "import signal; signal.pause()"]
    flooding = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"]

    timeout = execute(blocked, tmp_path, time.monotonic() + 0.5, Stop())
    overflow = execute(flooding, tmp_path, time.monotonic() + 10, Stop(), limit=1024)

    assert timeout.reason == "timeout"
    assert overflow.reason == "output_limit"
    assert len(overflow.output) <= 1024
    assert classify(timeout, tmp_path / "absent") == "timeout"
    assert classify(overflow, tmp_path / "absent") == "output_limit"


@pytest.mark.parametrize("directory", ["", "tests/.venv"])
def test_working_storage_overflow_is_inconclusive(
    tmp_path: Path, directory: str
) -> None:
    target = tmp_path / directory
    target.mkdir(parents=True, exist_ok=True)
    arguments = [
        sys.executable,
        "-c",
        "import signal; from pathlib import Path; "
        f"Path({str(target / 'large')!r}).write_bytes(b'x' * 200000); signal.pause()",
    ]

    result = execute(
        arguments,
        tmp_path,
        time.monotonic() + 10,
        Stop(),
        workspace=tmp_path,
        workspace_limit=4096,
    )

    assert result.reason == "workspace_limit"
    assert classify(result, tmp_path / "absent") == "workspace_limit"


PROCESS_TREE = """
import os
import signal
import subprocess
import sys
child: subprocess.Popen[bytes] = subprocess.Popen(
    [sys.executable, '-c', 'import signal; signal.pause()']
)
def stop(number: int, frame: object) -> None:
    child.wait(timeout=1)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
with open(sys.argv[1], 'w') as ready:
    ready.write(f'{os.getpid()} {child.pid}')
signal.pause()
"""


def test_cancellation_releases_real_parent_and_descendant(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    os.mkfifo(ready)
    descriptor = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    stop = Stop()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                execute,
                [sys.executable, "-c", PROCESS_TREE, str(ready)],
                tmp_path,
                time.monotonic() + 10,
                stop,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(descriptor, selectors.EVENT_READ)
                assert selector.select(5)
                pids = [int(value) for value in os.read(descriptor, 128).split()]

            stop.receive(15, None)
            result = pending.result(timeout=5)

        assert result.reason == "interrupted"
        assert len(pids) == 2
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        os.close(descriptor)


def test_child_environment_contains_only_explicit_values(tmp_path: Path) -> None:
    values = environment(tmp_path)

    result = execute(
        [sys.executable, "-c", "import os; print(sorted(os.environ))"],
        tmp_path,
        time.monotonic() + 10,
        Stop(),
    )

    assert result.code == 0
    assert b"GITHUB_TOKEN" not in result.output
    assert b"SSH_AUTH_SOCK" not in result.output
    assert values["HOME"] == str(tmp_path)


EARLY_EXIT = """
import subprocess
import sys
import os
child: subprocess.Popen[bytes] = subprocess.Popen(
    [sys.executable, '-c',
     "import signal,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
     "sys.stdout.write('r'); sys.stdout.flush(); signal.pause()"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
assert child.stdout is not None
assert child.stdout.read(1) == b'r'
child.stdout.close()
sys.stdout.write(f'{os.getpid()} {child.pid}')
"""


def test_early_leader_exit_still_observes_resistant_descendant_closure(
    tmp_path: Path,
) -> None:
    arguments = [sys.executable, "-c", EARLY_EXIT]

    result = execute(arguments, tmp_path, time.monotonic() + 10, Stop())

    assert result.code == 0
    for pid in (int(value) for value in result.output.split()):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_unconfirmed_closure_retains_owned_workspace(tmp_path: Path) -> None:
    def abandon() -> None:
        with workspace(tmp_path) as retained:
            (retained / "owned").write_text("retain")
            raise ClosureFailure("unconfirmed")

    with pytest.raises(ClosureFailure, match="unconfirmed"):
        abandon()

    retained = next((tmp_path / ".work").iterdir())
    assert (retained / "owned").read_text() == "retain"


def test_restoration_cannot_mask_unconfirmed_closure(tmp_path: Path) -> None:
    def abandon() -> None:
        with workspace(tmp_path) as retained:
            target = retained / "source.py"
            target.write_text("original")
            with restored_source(target):
                target.write_text("mutated")
                target.chmod(0o400)
                raise ClosureFailure("unconfirmed")

    with pytest.raises(ClosureFailure, match="unconfirmed"):
        abandon()

    retained = next((tmp_path / ".work").iterdir())
    assert (retained / "source.py").read_text() == "mutated"


def test_relative_output_produces_absolute_child_paths(tmp_path: Path) -> None:
    relative = Path(os.path.relpath(tmp_path, Path.cwd()))

    with workspace(relative) as retained:
        absolute = retained.is_absolute()

    assert absolute
    assert not list((tmp_path / ".work").iterdir())


@pytest.mark.parametrize("value", ["PRIVATE_AUDIT_MARKER", "a" * 40])
def test_entrypoint_failures_are_content_free(tmp_path: Path, value: str) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    variables = environment(ROOT)
    variables["PATH"] = str(Path(uv).parent) + os.pathsep + os.defpath
    arguments = [
        sys.executable,
        "-B",
        "-m",
        "scripts.ci.mutation_audit",
        "--source-revision",
        value,
        "--output",
        str(tmp_path),
    ]

    result = execute(
        arguments, ROOT, time.monotonic() + 10, Stop(), variables=variables
    )

    assert result.code == 1
    assert b'"event":"mutation.audit.failed"' in result.output
    assert value.encode() not in result.output
    assert b"Traceback" not in result.output


def test_publication_failure_is_content_free(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    git = shutil.which("git")
    assert uv is not None
    assert git is not None
    variables = environment(ROOT)
    variables["PATH"] = str(Path(uv).parent) + os.pathsep + os.defpath
    repository = tmp_path / "source"
    repository.mkdir()
    for operation in (
        ["init", "--initial-branch=master"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        ["update-ref", "refs/remotes/origin/master", "HEAD"],
    ):
        prepared = execute([git, *operation], repository, time.monotonic() + 10, Stop())
        assert prepared.code == 0
    source = (
        execute([git, "rev-parse", "HEAD"], repository, time.monotonic() + 10, Stop())
        .output.decode()
        .strip()
    )
    blocked = tmp_path / "PRIVATE_REPORT_MARKER"
    blocked.write_text("occupied")
    arguments = [
        sys.executable,
        "-B",
        "-c",
        "from pathlib import Path; from scripts.ci.mutation_audit import main; "
        f"raise SystemExit(main(Path({str(repository)!r})))",
        "--source-revision",
        source,
        "--output",
        str(blocked),
    ]

    result = execute(
        arguments, ROOT, time.monotonic() + 10, Stop(), variables=variables
    )

    assert result.code == 1
    assert b'"event":"mutation.audit.failed"' in result.output
    assert b"PRIVATE_REPORT_MARKER" not in result.output
    assert b"Traceback" not in result.output


def test_rotation_survives_earlier_inserted_sites_and_invalidated_inputs() -> None:
    previous = Report(
        source_revision=REVISION,
        tooling_revision=REVISION,
        next_identity="stable-b",
        cursor=1,
    )
    work = [mutation(0, "inserted"), mutation(1, "stable-a"), mutation(2, "stable-b")]

    queue = ordered(work, previous)

    assert queue[1].identity() == "stable-b"
    assert {item.identity() for item in queue} == {item.identity() for item in work}


@pytest.mark.parametrize("kind", ["source", "test", "helper", "lock", "config"])
def test_input_changes_invalidate_results(tmp_path: Path, kind: str) -> None:
    scope = Scope(source="src/value.py", tests="tests/test_value.py")
    names = {
        "source": "src/value.py",
        "test": "tests/test_value.py",
        "helper": "tests/helper.py",
        "lock": "uv.lock",
        "config": "pyproject.toml",
    }
    for name in names.values():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")
    original = digest(tmp_path, scope)

    (tmp_path / names[kind]).write_text("changed")
    changed = digest(tmp_path, scope)

    assert original != changed


def test_preexisting_bytecode_is_removed(tmp_path: Path) -> None:
    cache = tmp_path / "src/__pycache__"
    cache.mkdir(parents=True)
    (cache / "value.pyc").write_bytes(b"stale")

    clean_bytecode(tmp_path)

    assert not list(tmp_path.rglob("*.pyc"))


@pytest.mark.parametrize("kind", ["symlink", "traversal", "oversized"])
def test_source_archive_refuses_unsafe_members(tmp_path: Path, kind: str) -> None:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        info = tarfile.TarInfo("../escape" if kind == "traversal" else "source")
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/outside"
        elif kind == "oversized":
            info.size = 8 * 1024 * 1024 + 1
        archive.addfile(info)

    with pytest.raises(ValueError, match="archive"):
        unpack(content.getvalue(), tmp_path)

    assert not list(tmp_path.iterdir())


def test_partial_reports_preserve_pending_work_and_group_findings(
    tmp_path: Path,
) -> None:
    items = [mutation(index, str(index)) for index in range(3)]
    report = Report(
        source_revision=REVISION,
        tooling_revision=REVISION,
        results={
            item.identity(): Result(
                mutation=item,
                outcome="pending" if index == 0 else "survived",
                input_digest="input",
                tested_revision=REVISION,
            )
            for index, item in enumerate(items)
        },
    )

    save(report, tmp_path)
    restored = Report.model_validate_json((tmp_path / "report.json").read_bytes())
    summary = (tmp_path / "summary.md").read_text()

    assert restored.status == "partial"
    assert sum(value.outcome == "pending" for value in restored.results.values()) == 1
    assert summary.count("estimate_usage_usd") == 1
