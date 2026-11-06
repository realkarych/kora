import errno
import json
import os
import selectors
import signal
import subprocess
import sys
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Literal, cast

import pytest

from tests.cel_eval.children import ROOT, full_path, run_child
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.cel_sink_process import run_sink_child


@pytest.mark.parametrize(
    ("code", "expected", "overflow"),
    [
        ("import sys; sys.stdout.write('complete')", b"complete", False),
        ("import os; os.write(1, b'x' * 10000)", b"x" * 256, True),
    ],
)
def test_cel_measurement_children_are_bounded_and_reaped(
    code: str, expected: bytes, overflow: bool
) -> None:
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_process([sys.executable, "-I", "-c", code], output_limit=256)

    assert result.stdout == expected
    assert result.timed_out is False
    assert result.overflow is overflow
    assert len(os.listdir("/dev/fd")) == descriptors_before
    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)


def test_cel_measurement_child_past_its_deadline_is_killed_and_reaped() -> None:
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_process(
        [sys.executable, "-I", "-c", "import signal; signal.pause()"],
        deadline_seconds=0.2,
        output_limit=256,
    )

    assert result.stdout == b""
    assert result.timed_out is True
    assert result.overflow is False
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
import json, os, signal, sys
from pathlib import Path
sys.path[:] = json.loads(sys.argv[1])
from tests.measurements.cel_measurement_process import run_process
child = (
    "import os, signal, sys; from pathlib import Path; "
    "Path(sys.argv[1]).write_text(str(os.getpid())); "
    "os.kill(os.getppid(), signal.SIGINT); signal.pause()"
)
before = len(os.listdir('/dev/fd'))
handler_before = signal.getsignal(signal.SIGINT)
try:
    run_process([sys.executable, '-I', '-c', child, sys.argv[2]])
except KeyboardInterrupt:
    assert signal.getsignal(signal.SIGINT) is handler_before
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


def assert_selector_interrupt_closes(
    supervisor: Literal["measurement", "sink"],
) -> None:
    child_code = 'import os\nwhile True:\n    os.write(1, b"x" * 65536)\n'
    launch = (
        partial(run_process, [sys.executable, "-I", "-c", child_code])
        if supervisor == "measurement"
        else partial(run_sink_child, child_code, full_path(ROOT), deadline_seconds=10.0)
    )
    selector_init = selectors.DefaultSelector.__init__.__code__
    observed: tuple[int, int] | None = None

    def interrupt_after_allocation(frame: FrameType, event: str, value: object) -> None:
        nonlocal observed
        if event != "return" or frame.f_code is not selector_init:
            return
        caller = frame.f_back
        if caller is None or caller.f_code is not launch.func.__code__:
            return
        assert observed is None
        selector = cast(selectors.DefaultSelector, frame.f_locals["self"])
        child = cast(subprocess.Popen[bytes], caller.f_locals["child"])
        observed = (selector.fileno(), child.pid)
        signal.raise_signal(signal.SIGINT)

    descriptors_before = len(os.listdir("/dev/fd"))
    handler_before = signal.getsignal(signal.SIGINT)
    observer_before = sys.getprofile()
    sys.setprofile(interrupt_after_allocation)
    try:
        with pytest.raises(KeyboardInterrupt) as interruption:
            launch()
    finally:
        sys.setprofile(observer_before)

    assert interruption.value.__traceback__ is not None
    assert observed is not None
    descriptor, pid = observed
    with pytest.raises(OSError, match=rf"\[Errno {errno.EBADF}\]") as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert signal.getsignal(signal.SIGINT) is handler_before
    assert len(os.listdir("/dev/fd")) == descriptors_before
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize("supervisor", ["measurement", "sink"])
def test_cel_selector_startup_interrupt_closes_native_descriptor(
    supervisor: Literal["measurement", "sink"],
) -> None:
    code = (
        "from tests.measurements.test_cel_measurement_process "
        "import assert_selector_interrupt_closes\n"
        "assert_selector_interrupt_closes(sys.argv[2])\n"
    )

    result = run_child(code, full_path(ROOT), supervisor)

    assert result.code == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
