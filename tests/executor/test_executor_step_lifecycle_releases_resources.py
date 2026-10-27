import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.executor.captures import Captures
from kora.executor.failures import ExecutorError
from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher
from kora.executor.steps import run_step
from kora.ir.node import ErrorClass
from tests.executor.step_support import PYTHON, context, step

FAST: ProcessLauncher = ProcessLauncher(
    kill_grace_ns=200_000_000, eof_bound_ns=400_000_000
)
REPORT_PID: str = "import os, sys, time\nprint(os.getpid(), flush=True)\n"
TIMED_OUT_STEP_NS: int = 300_000_000
EXITING_STEP_NS: int = 30_000_000_000


def descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def not_our_child(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def spec(code: str, timeout_ns: int, cwd: Path) -> LaunchSpec:
    return LaunchSpec(
        argv=(PYTHON, "-I", "-c", code),
        cwd=cwd,
        env={},
        stdin=None,
        timeout_ns=timeout_ns,
        stdout_limit=4096,
        stderr_limit=4096,
    )


def pid_of(output: bytes) -> int:
    return int(output.split()[0])


@pytest.mark.parametrize(
    ("code", "timeout_ns", "ending"),
    [
        (REPORT_PID + "time.sleep(30)", TIMED_OUT_STEP_NS, Ending.TIMED_OUT),
        (REPORT_PID + "sys.exit(3)", EXITING_STEP_NS, Ending.EXITED),
        (
            REPORT_PID
            + "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + "time.sleep(30)",
            TIMED_OUT_STEP_NS,
            Ending.TIMED_OUT,
        ),
    ],
)
def test_executor_step_lifecycle_releases_resources(
    tmp_path: Path, code: str, timeout_ns: int, ending: Ending
) -> None:
    baseline = descriptors()

    outcome = asyncio.run(FAST.run(spec(code, timeout_ns, tmp_path)))

    pid = pid_of(outcome.stdout)
    assert outcome.ending is ending
    assert gone(pid)
    assert not_our_child(pid)
    assert descriptors() == baseline


def test_cancellation_reaps_the_child(tmp_path: Path) -> None:
    baseline = descriptors()
    marker = tmp_path / "pid"
    code = (
        f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)"
    )

    async def cancelled() -> None:
        task = asyncio.create_task(FAST.run(spec(code, 30_000_000_000, tmp_path)))
        deadline = time.monotonic() + 10
        while not (marker.exists() and marker.read_text()):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())

    pid = int(marker.read_text())
    assert gone(pid)
    assert not_our_child(pid)
    assert descriptors() == baseline


def test_a_descendant_holding_a_pipe_leaves_the_step_unknown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    subject = context(kora_capture, tmp_path, launcher=FAST)
    record = tmp_path / "grandchild"
    holder = step(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'])"
        f"\nopen({str(record)!r}, 'w').write(str(child.pid))\n",
        timeout=timedelta(seconds=30),
    )
    baseline = descriptors()

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(run_step(holder, 0, subject, Captures()))
    grandchild = int(record.read_text())
    deadline = time.monotonic() + 10
    while not gone(grandchild):
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert raised.value.code == "step_outcome_unknown"
    assert raised.value.failure_class is ErrorClass.INFRASTRUCTURE
    assert descriptors() == baseline
