from typing import cast

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
