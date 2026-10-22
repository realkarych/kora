import json
import os
import sys
from pathlib import Path

import pytest

from tests.cel_eval.children import ROOT, full_path
from tests.measurements.cel_measurement_process import run_process


@pytest.mark.parametrize(
    ("code", "expected", "timed_out", "overflow"),
    [
        ("import sys; sys.stdout.write('complete')", b"complete", False, False),
        ("import signal; signal.pause()", b"", True, False),
        ("import os; os.write(1, b'x' * 10000)", b"x" * 256, False, True),
    ],
)
def test_cel_measurement_children_are_bounded_and_reaped(
    code: str, expected: bytes, timed_out: bool, overflow: bool
) -> None:
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_process(
        [sys.executable, "-I", "-c", code],
        deadline_seconds=0.2,
        output_limit=256,
    )

    assert result.stdout == expected
    assert result.timed_out is timed_out
    assert result.overflow is overflow
    assert len(os.listdir("/dev/fd")) == descriptors_before
    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)


def test_cel_measurement_child_environment_is_explicit() -> None:
    code = "import os, sys; sys.stdout.write(','.join(sorted(os.environ)))"
    permitted = {"PATH", "LANG", "LC_ALL"}
    if sys.platform == "darwin":
        permitted.add("__CF_USER_TEXT_ENCODING")

    result = run_process([sys.executable, "-I", "-c", code])

    assert result.code == 0
    assert set(result.stdout.decode().split(",")) <= permitted
    assert not result.stderr


def test_cel_measurement_child_failure_retains_bounded_stderr() -> None:
    code = "import os, sys; os.write(2, b'failure'); sys.exit(7)"

    result = run_process([sys.executable, "-I", "-c", code])

    assert result.code == 7
    assert result.stderr == b"failure"
    assert not result.stdout


def test_cel_measurement_interrupt_kills_and_reaps_its_child(tmp_path: Path) -> None:
    code = """
import json, os, sys
from pathlib import Path
sys.path[:] = json.loads(sys.argv[1])
from tests.measurements.cel_measurement_process import run_process
child = (
    "import os, signal, sys; from pathlib import Path; "
    "Path(sys.argv[1]).write_text(str(os.getpid())); "
    "os.kill(os.getppid(), signal.SIGINT); signal.pause()"
)
before = len(os.listdir('/dev/fd'))
try:
    run_process([sys.executable, '-I', '-c', child, sys.argv[2]])
except KeyboardInterrupt:
    pid = int(Path(sys.argv[2]).read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        assert len(os.listdir('/dev/fd')) == before
        sys.stdout.write('released')
    else:
        raise RuntimeError('interrupted child survived')
else:
    raise RuntimeError('interrupt was not delivered')
"""

    result = run_process(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            json.dumps(full_path(ROOT)),
            str(tmp_path / "child.pid"),
        ]
    )

    assert result.code == 0
    assert result.stdout == b"released"
    assert not result.stderr
