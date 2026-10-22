import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: bytes
    stderr: bytes

    def stderr_lines(self) -> list[str]:
        return self.stderr.decode("utf-8").splitlines()

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stderr_lines()]


def run_python(arguments: list[str], environment: Mapping[str, str]) -> ProcessResult:
    completed = subprocess.run(
        [sys.executable, *arguments],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        timeout=60,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)
