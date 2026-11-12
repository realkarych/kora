import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import median

from scripts.ci.lane_plan import Durations, load_table
from tests.scaffold_evidence import (
    LOCAL_RUNTIME_PATH,
    STORAGE_ARGUMENTS,
    environment_key,
    scheduled_modules,
)

OPERATING_SYSTEM = "ubuntu-24.04"
PYTHON = "3.11.17"
ROUNDS = 40
WINDOW_SECONDS = 300.0
OUTPUT_TAIL_BYTES = 65536
NOTHING_COLLECTED = 5
ANNOTATION = "Changed test repetition"
PLAIN_PATH = re.compile(r"[A-Za-z0-9_./-]+")

Command = tuple[str, ...]
Launcher = Callable[[str], Command]
Clock = Callable[[], float]


class RepetitionFailure(Exception):
    pass


@dataclass(frozen=True)
class Module:
    path: str
    added: bool
    estimate: float


@dataclass(frozen=True)
class Outcome:
    passed: bool
    output: str


@dataclass(frozen=True)
class Limits:
    rounds: int = ROUNDS
    window: float = WINDOW_SECONDS


@dataclass(frozen=True)
class Report:
    executions: dict[str, int]
    failed: str | None
    output: str


Execute = Callable[[Module], Outcome]


def git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RepetitionFailure("git is required")
    completed = subprocess.run(
        [executable, "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return completed.stdout.decode("utf-8")


def is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/") and name.startswith("test_") and name.endswith(".py")
    )


def changed_test_modules(repository: Path, base: str, head: str) -> dict[str, bool]:
    listing = git(
        repository,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--diff-filter=AMR",
        base,
        head,
        "--",
        "tests",
    )
    fields = iter(listing.split("\0"))
    changed: dict[str, bool] = {}
    for status in fields:
        if not status:
            continue
        path = next(fields)
        if status.startswith("R"):
            path = next(fields)
        if is_test_module(path):
            if PLAIN_PATH.fullmatch(path) is None:
                raise RepetitionFailure("a changed test module has an unusual path")
            changed[path] = status == "A"
    return dict(sorted(changed.items()))


def pull_request_changes(repository: Path) -> dict[str, bool]:
    parents = git(repository, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if len(parents) != 2:
        raise RepetitionFailure("the checkout is not a pull request merge commit")
    return changed_test_modules(repository, parents[0], "HEAD")


def lane_of(path: str) -> str:
    if path == LOCAL_RUNTIME_PATH or path.startswith(f"{LOCAL_RUNTIME_PATH}/"):
        return "local-runtime"
    if any(
        path == owned or path.startswith(f"{owned}/") for owned in STORAGE_ARGUMENTS
    ):
        return "storage"
    return "general"


def lane_command(path: str) -> Command:
    lane = lane_of(path)
    if lane == "local-runtime":
        return (
            "bash",
            "scripts/ci/run-local-runtime-tests.sh",
            "uv",
            "run",
            "--no-sync",
            "pytest",
            "-q",
            path,
        )
    if lane == "storage":
        return ("uv", "run", "--no-sync", "pytest", "-q", path)
    return (
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "tests.support.ordinary_pytest",
        "-q",
        path,
    )


def collected_modules(
    paths: Sequence[str], collector: Command, directory: Path
) -> frozenset[str]:
    if not paths:
        return frozenset()
    completed = subprocess.run(
        [*collector, "--collect-only", "-q", *paths],
        cwd=directory,
        check=False,
        capture_output=True,
        timeout=120,
    )
    listing = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode == NOTHING_COLLECTED:
        return frozenset()
    if completed.returncode != 0:
        raise RepetitionFailure(
            "the changed test modules cannot be collected\n"
            + listing[-OUTPUT_TAIL_BYTES:]
        )
    collected = frozenset(
        line.partition("::")[0] for line in listing.splitlines() if "::" in line
    )
    if not collected or not collected <= frozenset(paths):
        raise RepetitionFailure("the collection of changed test modules is unreadable")
    return collected


def ordered(changes: dict[str, bool], durations: Durations) -> tuple[Module, ...]:
    unrecorded = median(record.milliseconds for record in durations.values()) / 1000
    modules = [
        Module(
            path,
            added,
            durations[path].milliseconds / 1000 if path in durations else unrecorded,
        )
        for path, added in changes.items()
    ]
    return tuple(
        sorted(
            modules, key=lambda module: (not module.added, module.estimate, module.path)
        )
    )


def selection(
    changes: dict[str, bool],
    durations: Durations,
    collector: Command,
    directory: Path,
) -> tuple[Module, ...]:
    scheduled = frozenset(scheduled_modules(OPERATING_SYSTEM))
    candidates = [path for path in changes if path not in scheduled]
    collected = collected_modules(candidates, collector, directory)
    return ordered(
        {path: added for path, added in changes.items() if path in collected},
        durations,
    )


def repetition(
    modules: Sequence[Module], execute: Execute, clock: Clock, limits: Limits
) -> Report:
    started = clock()
    executions = {module.path: 0 for module in modules}
    longest = {module.path: module.estimate for module in modules}
    for _ in range(limits.rounds):
        progressed = False
        for module in modules:
            begun = clock()
            if begun - started + longest[module.path] > limits.window:
                continue
            outcome = execute(module)
            spent = clock() - begun
            executions[module.path] += 1
            if executions[module.path] == 1 or spent > longest[module.path]:
                longest[module.path] = spent
            if not outcome.passed:
                return Report(executions, module.path, outcome.output)
            progressed = True
        if not progressed:
            break
    return Report(executions, None, "")


def process_execution(launcher: Launcher, directory: Path, logs: Path) -> Execute:
    def execute(module: Module) -> Outcome:
        log = logs / f"execution-{len(list(logs.iterdir())) + 1}.log"
        with log.open("wb") as stream:
            completed = subprocess.run(
                launcher(module.path),
                cwd=directory,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        if completed.returncode == 0:
            return Outcome(True, "")
        recorded = log.read_bytes()[-OUTPUT_TAIL_BYTES:]
        return Outcome(False, recorded.decode("utf-8", errors="replace"))

    return execute


def summary(report: Report, unselected: Sequence[str]) -> str:
    total = sum(report.executions.values())
    lines = [
        f"## {ANNOTATION}",
        "",
        f"{len(report.executions)} selected test modules, {total} executions.",
        "",
    ]
    if report.executions:
        lines += [
            "| Module | Executions |",
            "|---|---|",
            *(f"| `{path}` | {count} |" for path, count in report.executions.items()),
            "",
        ]
    if unselected:
        lines += [
            "Changed but not repeated, because the daily workflow runs them or "
            "the default test selection leaves all their tests out:",
            "",
            *(f"- `{path}`" for path in unselected),
            "",
        ]
    if report.failed is not None:
        lines += [f"An execution of `{report.failed}` failed.", ""]
    return "\n".join(lines)


def annotations(report: Report) -> str:
    lines = [
        f"::warning title={ANNOTATION}::{path} was not repeated: one execution "
        "does not fit the repetition window\n"
        for path, count in report.executions.items()
        if count == 0 and report.failed is None
    ]
    if report.failed is not None:
        lines.append(
            f"::error title={ANNOTATION}::an execution of {report.failed} failed; "
            "a failed execution is never retried\n"
        )
    return "".join(lines)


def repeat(repository: Path) -> int:
    changes = pull_request_changes(repository)
    durations = load_table().environments[environment_key(OPERATING_SYSTEM, PYTHON)]
    collector = ("uv", "run", "--no-sync", "pytest")
    modules = selection(changes, durations, collector, repository)
    with tempfile.TemporaryDirectory() as logs:
        execute = process_execution(lane_command, repository, Path(logs))
        report = repetition(modules, execute, time.monotonic, Limits())
    unselected = sorted(changes.keys() - report.executions.keys())
    rendered = summary(report, unselected)
    sys.stdout.write(report.output)
    sys.stdout.write(annotations(report))
    sys.stdout.write(rendered)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a") as stream:
            stream.write(rendered)
    return 0 if report.failed is None else 1


def main(arguments: list[str]) -> int:
    match arguments:
        case ["repeat"]:
            return repeat(Path.cwd())
        case _:
            raise RepetitionFailure("unknown changed test repetition operation")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
