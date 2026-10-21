import json
import subprocess
import sys

from tests.cel_eval.children import (
    CHILD_TIMEOUT_SECONDS,
    PRELUDE,
    ROOT,
    ChildResult,
    full_path,
)


def captured_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_native(traced: bool, seed: int) -> ChildResult:
    code = (
        "from tests.measurements.cel_workload import run\n"
        f"print(run('native', {traced!r}))\n"
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-s",
                "-P",
                "-c",
                PRELUDE + code,
                json.dumps(full_path(ROOT)),
            ],
            env={"PYTHONHASHSEED": str(seed), "LC_ALL": "C.UTF-8"},
            capture_output=True,
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        return ChildResult(-1, captured_text(error.stdout), captured_text(error.stderr))
    return ChildResult(result.returncode, result.stdout, result.stderr)
