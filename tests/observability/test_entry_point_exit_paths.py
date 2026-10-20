import os
import shutil
import subprocess
import sys
from typing import cast

import pytest

from tests.observability.conftest import run_kora, run_python

FAILING_APPLICATION_SCRIPT: str = """
import sys
import typer
from kora.__main__ import main

def explode() -> None:
    hidden_local = "LOCAL-MARKER"
    try:
        raise KeyError("CAUSE-MARKER")
    except KeyError as cause:
        raise RuntimeError("MESSAGE-MARKER " + hidden_local) from cause

app = typer.Typer(pretty_exceptions_enable=False)

@app.command()
def boom() -> None:
    explode()

@app.command()
def other() -> None:
    return None

sys.exit(main(["boom"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""

INTERRUPTED_APPLICATION_SCRIPT: str = """
import sys
import typer
from kora.__main__ import main

app = typer.Typer(pretty_exceptions_enable=False)

@app.command()
def wait() -> None:
    raise KeyboardInterrupt()

@app.command()
def other() -> None:
    return None

sys.exit(main(["wait"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""


def test_version_exits_zero_with_stdout_only() -> None:
    result = run_kora(["--version"], {})

    assert (result.code, result.stdout, result.stderr) == (0, b"kora 0.1.0.dev0\n", b"")


def test_usage_errors_exit_two() -> None:
    result = run_kora(["--nope"], {})

    assert result.code == 2
    assert result.stdout == b""
    assert result.stderr_lines()[0].startswith("Usage: kora")


def test_invalid_configuration_exits_seventy_eight() -> None:
    result = run_kora(["--version"], {"KORA_LOG_LEVEL": "verbose"})

    assert result.code == 78
    assert result.stdout == b""
    assert len(result.stderr_lines()) == 1


def test_unhandled_error_exits_seventy_with_one_safe_record() -> None:
    result = run_python(["-c", FAILING_APPLICATION_SCRIPT], {})

    records = result.records()
    failed = [record for record in records if record["event"] == "process.failed"]
    stderr_text = result.stderr.decode("utf-8")
    assert result.code == 70
    assert result.stdout == b""
    assert len(failed) == 1
    assert failed[0]["level"] == "CRITICAL"
    assert failed[0]["exit_code"] == 70
    chain = cast(list[dict[str, object]], failed[0]["exception"])
    frames = cast(list[dict[str, object]], chain[0]["frames"])
    assert [entry["type"] for entry in chain] == ["RuntimeError", "KeyError"]
    assert frames[-1]["function"] == "explode"
    assert all("line" in frame for frame in frames)
    assert "MARKER" not in stderr_text
    assert "hidden_local" not in stderr_text
    assert "raise RuntimeError" not in stderr_text
    assert records[-1]["event"] == "process.stopped"
    assert records[-1]["exit_code"] == 70


def test_interrupt_exits_one_hundred_thirty_without_a_failure_record() -> None:
    result = run_python(["-c", INTERRUPTED_APPLICATION_SCRIPT], {})

    events = [record["event"] for record in result.records()]
    assert result.code == 130
    assert result.stdout == b""
    assert "process.failed" not in events
    assert events[-1] == "process.stopped"
    assert result.records()[-1]["exit_code"] == 130


EXITING_APPLICATION_SCRIPT: str = """
import sys
import typer
from kora.__main__ import main

app = typer.Typer(pretty_exceptions_enable=False)

@app.command()
def leave() -> None:
    raise EXIT_REQUEST

@app.command()
def other() -> None:
    return None

sys.exit(main(["leave"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""

BASE_EXCEPTION_APPLICATION_SCRIPT: str = """
import sys
import typer
from kora.__main__ import main

class Unusual(BaseException):
    pass

app = typer.Typer(pretty_exceptions_enable=False)

@app.command()
def odd() -> None:
    raise Unusual("BASE-MARKER")

@app.command()
def other() -> None:
    return None

sys.exit(main(["odd"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""

THREAD_FAILURE_APPLICATION_SCRIPT: str = """
import sys
import threading
import typer
from kora.__main__ import main

app = typer.Typer(pretty_exceptions_enable=False)

def fail() -> None:
    raise ValueError("THREAD-MARKER")

@app.command()
def spawn() -> None:
    worker = threading.Thread(target=fail)
    worker.start()
    worker.join()

@app.command()
def other() -> None:
    return None

sys.exit(main(["spawn"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""

BROKEN_STDOUT_SCRIPT: str = """
import os
import sys
from kora.__main__ import main

read_end, write_end = os.pipe()
os.dup2(write_end, 1)
os.close(read_end)
os.close(write_end)
sys.exit(main(["--version"], {"KORA_LOG_LEVEL": "DEBUG"}))
"""

CLOSING_STDOUT_APPLICATION_SCRIPT: str = """
import os
import sys
import typer
from kora.__main__ import main

app = typer.Typer(pretty_exceptions_enable=False)

@app.command()
def close() -> None:
    sys.stdout.write("buffered")
    os.close(1)

@app.command()
def other() -> None:
    return None

sys.exit(main(["close"], {"KORA_LOG_LEVEL": "DEBUG"}, app))
"""


@pytest.mark.parametrize(
    ("exit_request", "exit_code"),
    [
        ("SystemExit(3)", 3),
        ("SystemExit(-1)", 255),
        ("SystemExit(True)", 1),
        ("typer.Exit(300)", 44),
    ],
)
def test_system_exit_from_a_command_keeps_its_code(
    exit_request: str, exit_code: int
) -> None:
    script = EXITING_APPLICATION_SCRIPT.replace("EXIT_REQUEST", exit_request)

    result = run_python(["-c", script], {})

    events = [record["event"] for record in result.records()]
    assert result.code == exit_code
    assert result.stdout == b""
    assert "process.failed" not in events
    assert events[-1] == "process.stopped"
    assert result.records()[-1]["exit_code"] == exit_code


def test_base_exception_is_recorded_and_exits_seventy() -> None:
    result = run_python(["-c", BASE_EXCEPTION_APPLICATION_SCRIPT], {})

    records = result.records()
    failed = [record for record in records if record["event"] == "process.failed"]
    assert result.code == 70
    assert len(failed) == 1
    assert "BASE-MARKER" not in result.stderr.decode("utf-8")
    assert records[-1]["exit_code"] == 70


def test_thread_failure_is_recorded_without_failing_the_process() -> None:
    result = run_python(["-c", THREAD_FAILURE_APPLICATION_SCRIPT], {})

    records = result.records()
    events = [record["event"] for record in records]
    assert result.code == 0
    assert events.count("thread.failed") == 1
    assert "process.failed" not in events
    assert "THREAD-MARKER" not in result.stderr.decode("utf-8")
    thread_failed = next(r for r in records if r["event"] == "thread.failed")
    assert "exit_code" not in thread_failed
    assert records[-1]["exit_code"] == 0


@pytest.mark.parametrize(
    "script", [BROKEN_STDOUT_SCRIPT, CLOSING_STDOUT_APPLICATION_SCRIPT]
)
def test_broken_stdout_exits_one_without_a_failure_record(script: str) -> None:
    result = run_python(["-c", script], {})

    events = [record["event"] for record in result.records()]
    stderr_text = result.stderr.decode("utf-8")
    assert result.code == 1
    assert "process.failed" not in events
    assert "Exception ignored" not in stderr_text
    assert "Error" not in stderr_text
    assert result.records()[-1]["exit_code"] == 1


def shell() -> str:
    found = shutil.which("sh")
    assert found is not None
    return found


@pytest.mark.parametrize(
    ("redirection", "exit_code"), [("1>&-", 0), ("1</dev/null", 70)]
)
def test_unusable_stdout_descriptor_still_shuts_down_cleanly(
    redirection: str, exit_code: int
) -> None:
    completed = subprocess.run(
        [shell(), "-c", f'exec "$0" -m kora --version {redirection}', sys.executable],
        env={"PATH": os.environ["PATH"], "KORA_LOG_LEVEL": "DEBUG"},
        capture_output=True,
        timeout=60,
        check=False,
    )

    lines = completed.stderr.decode("utf-8").splitlines()
    failures = [line for line in lines if '"event":"process.failed"' in line]
    assert completed.returncode == exit_code
    assert all(line.startswith("{") for line in lines)
    assert "Traceback" not in completed.stderr.decode("utf-8")
    assert '"event":"process.stopped"' in lines[-1]
    assert f'"exit_code":{exit_code}' in lines[-1]
    assert len(failures) == (1 if exit_code == 70 else 0)


@pytest.mark.parametrize(
    ("arguments", "environment", "exit_code"),
    [
        ("--nope", {}, 2),
        ("--version", {"KORA_LOG_LEVEL": "verbose"}, 78),
        ("--version", {"KORA_LOG_LEVEL": "DEBUG"}, 0),
    ],
)
def test_closed_stderr_keeps_the_exit_code(
    arguments: str, environment: dict[str, str], exit_code: int
) -> None:
    completed = subprocess.run(
        [shell(), "-c", f'exec "$0" -m kora {arguments} 2>&-', sys.executable],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == exit_code
    assert completed.stderr == b""
    assert completed.stdout == (b"kora 0.1.0.dev0\n" if exit_code == 0 else b"")
