import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

import pytest

from tests.cel_eval.children import ROOT, full_path, run_child
from tests.measurements.cel_sink_process import READY, SinkProcessResult, run_sink_child
from tests.measurements.harness import new_report
from tests.measurements.test_cel_evaluator_lifetime import (
    SinkPayload,
    sink_exit_gate,
    sink_gates,
)

CLEANUP: str = (
    "from tests.measurements.cel_sink_process import begin_cleanup\nbegin_cleanup()\n"
)


class LauncherPayload(TypedDict):
    environment_sha256: str
    session: int
    group: int


def assert_released(result: SinkProcessResult, descriptors_before: int) -> None:
    assert len(os.listdir("/dev/fd")) == descriptors_before
    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(result.pid, 0)


def test_cel_sink_exit_interval_starts_before_acknowledged_cleanup() -> None:
    code = CLEANUP + "import os; print(os.read(0, 1).hex())\n"
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_sink_child(code, full_path(ROOT))

    assert result.code == 0
    assert result.stdout == b"\n"
    assert result.stderr == b""
    assert result.readiness == READY
    assert result.post_work_seconds is not None
    assert 0 < result.post_work_seconds < result.total_seconds
    assert not result.timed_out
    assert not result.overflow
    assert not result.protocol_error
    sink_exit_gate(new_report(), "fixture", result)
    assert_released(result, descriptors_before)


def test_cel_sink_workload_barrier_keeps_sampling_and_terminal_cleanup() -> None:
    code = (
        "from tests.measurements.cel_sink_workload import run\n"
        "print(run('blocked', 'INFO', synchronize_cleanup=True))\n"
    )
    descriptors_before = len(os.listdir("/dev/fd"))
    report = new_report()

    result = run_sink_child(code, full_path(ROOT))

    sink_exit_gate(report, "fixture", result)
    payload: SinkPayload = json.loads(result.stdout)
    assert [point["cycle"] for point in payload["points"]] == list(range(10, 201, 10))
    assert all(
        point["outstanding"] == point["handles"] == point["payload_bytes"] == 0
        for point in payload["points"]
    )
    assert payload["stats"]["dropped"] > 0
    sink_gates(report, "fixture", payload, True)
    assert_released(result, descriptors_before)


@pytest.mark.parametrize("phase", ["workload", "cleanup", "interpreter_exit"])
def test_cel_sink_stalled_children_refuse_success_and_are_reaped(phase: str) -> None:
    code = "import signal\nsignal.pause()\n"
    if phase == "cleanup":
        code = CLEANUP + code
    elif phase == "interpreter_exit":
        code = (
            CLEANUP
            + "import atexit, signal\n"
            + "atexit.register(signal.pause)\n"
            + "print('{\"shutdown_seconds\":0}', flush=True)\n"
        )
    descriptors_before = len(os.listdir("/dev/fd"))
    report = new_report()

    result = run_sink_child(
        code,
        full_path(ROOT),
        deadline_seconds=0.2 if phase == "workload" else 60,
        exit_seconds=0.2,
    )

    assert result.timed_out
    assert not result.overflow
    assert not result.protocol_error
    assert (result.post_work_seconds is None) is (phase == "workload")
    if phase == "interpreter_exit":
        assert json.loads(result.stdout) == {"shutdown_seconds": 0}
    with pytest.raises(AssertionError, match="fixture_post_work_exit"):
        sink_exit_gate(report, "fixture", result)
    assert report.gates["fixture_post_work_exit"] is False
    assert report.environment["fixture_timed_out"] is True
    assert_released(result, descriptors_before)


@pytest.mark.parametrize("code", ["", "print('{}')", "print('wrong')", "print('x'*33)"])
def test_cel_sink_missing_or_malformed_readiness_refuses_success(code: str) -> None:
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_sink_child(code, full_path(ROOT))

    assert result.protocol_error
    assert result.post_work_seconds is None
    assert len(result.readiness) <= 33
    with pytest.raises(AssertionError, match="fixture_post_work_exit"):
        sink_exit_gate(new_report(), "fixture", result)
    assert_released(result, descriptors_before)


@pytest.mark.parametrize("descriptor", [1, 2])
def test_cel_sink_output_overflow_refuses_success_and_is_reaped(
    descriptor: int,
) -> None:
    code = CLEANUP + f"import os; os.write({descriptor}, b'x' * 10000)\n"
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_sink_child(code, full_path(ROOT), output_limit=256)

    assert result.overflow
    assert not result.timed_out
    assert not result.protocol_error
    assert len(result.stdout) + len(result.readiness) <= 256
    assert len(result.stderr) <= 256
    assert len(result.stdout if descriptor == 1 else result.stderr) == (
        256 - len(READY) if descriptor == 1 else 256
    )
    with pytest.raises(AssertionError, match="fixture_post_work_exit"):
        sink_exit_gate(new_report(), "fixture", result)
    assert_released(result, descriptors_before)


def test_cel_sink_nonzero_exit_retains_failure_and_refuses_success() -> None:
    code = CLEANUP + "import os; os.write(2, b'failure'); raise SystemExit(7)\n"
    descriptors_before = len(os.listdir("/dev/fd"))
    report = new_report()

    result = run_sink_child(code, full_path(ROOT))

    assert result.code == 7
    assert result.stderr == b"failure"
    assert not result.timed_out
    assert not result.overflow
    assert not result.protocol_error
    with pytest.raises(AssertionError, match="fixture_post_work_exit"):
        sink_exit_gate(report, "fixture", result)
    assert report.environment["fixture_stderr"] == "failure"
    assert report.environment["fixture_exit_code"] == 7
    assert_released(result, descriptors_before)


def test_cel_sink_launcher_inherits_environment_and_owns_its_session() -> None:
    code = (
        CLEANUP
        + "import hashlib, json, os, sys\n"
        + "selected = {name: os.environ[name] for name in json.loads(sys.argv[2])}\n"
        + "digest = hashlib.sha256("
        "json.dumps(selected, sort_keys=True).encode()).hexdigest()\n"
        + "print(json.dumps({'environment_sha256': digest, "
        + "'session': os.getsid(0), 'group': os.getpgrp()}))\n"
    )
    expected_digest = hashlib.sha256(
        json.dumps(dict(os.environ), sort_keys=True).encode()
    ).hexdigest()
    descriptors_before = len(os.listdir("/dev/fd"))

    result = run_sink_child(code, full_path(ROOT), json.dumps(sorted(os.environ)))

    sink_exit_gate(new_report(), "fixture", result)
    payload: LauncherPayload = json.loads(result.stdout)
    assert payload["environment_sha256"] == expected_digest
    assert payload["session"] == result.pid
    assert payload["group"] == result.pid
    assert_released(result, descriptors_before)


@pytest.mark.parametrize("phase", ["workload", "cleanup"])
def test_cel_sink_interrupt_kills_reaps_and_restores_signal_handler(
    tmp_path: Path, phase: str
) -> None:
    code = """
import os, signal, sys
from pathlib import Path
from tests.cel_eval.children import ROOT, full_path
from tests.measurements.cel_sink_process import run_sink_child
child = (
    "import os, signal, sys; from pathlib import Path; "
    "Path(sys.argv[2]).write_text(str(os.getpid())); "
)
if sys.argv[3] == 'cleanup':
    child += (
        "from tests.measurements.cel_sink_process import begin_cleanup; "
        "begin_cleanup(); "
    )
child += "os.kill(os.getppid(), signal.SIGINT); signal.pause()"
before = len(os.listdir('/dev/fd'))
handler_before = signal.getsignal(signal.SIGINT)
try:
    run_sink_child(child, full_path(ROOT), sys.argv[2])
except KeyboardInterrupt:
    assert signal.getsignal(signal.SIGINT) is handler_before
    pid = int(Path(sys.argv[2]).read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError('interrupted child survived')
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        assert len(os.listdir('/dev/fd')) == before
        sys.stdout.write('released')
    else:
        raise RuntimeError('interrupted child was not reaped')
else:
    raise RuntimeError('interrupt was not delivered')
"""

    result = run_child(code, full_path(ROOT), str(tmp_path / "child.pid"), phase)

    assert result.code == 0, result.stderr
    assert result.stdout == "released"
    assert result.stderr == ""
