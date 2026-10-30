import argparse
import ast
import hashlib
import io
import json
import math
import os
import shutil
import signal
import sys
import tarfile
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from typing import NoReturn
from xml.parsers import expat

from pydantic import TypeAdapter

from scripts.ci.mutation_models import (
    MAX_FILE,
    MAX_MUTANTS,
    MAX_OUTPUT,
    MAX_WORK,
    SCOPES,
    Mutation,
    Outcome,
    Report,
    Result,
    Scope,
    Work,
    digest,
    ordered,
    regular_bytes,
    rotation_order,
)
from scripts.ci.mutation_process import (
    ClosureFailure,
    Execution,
    Stop,
    environment,
    execute,
    workspace_bytes,
)

ROOT: Path = Path(__file__).resolve().parents[2]
WORK: TypeAdapter[tuple[Work, None]] = TypeAdapter(tuple[Work, None])


class CommandFailure(RuntimeError):
    def __init__(self, result: Execution, executable: str) -> None:
        self.reason = f"command_{Path(executable).name}_{result.reason}_{result.code}"
        super().__init__(self.reason)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid audit arguments")


@contextmanager
def workspace(output: Path) -> Generator[Path, None, None]:
    parent = output.resolve() / ".work"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(mkdtemp(prefix="audit-", dir=parent))
    release = True
    try:
        yield root
    except ClosureFailure:
        release = False
        raise
    finally:
        if release:
            shutil.rmtree(root)


@contextmanager
def restored_source(target: Path) -> Generator[None, None, None]:
    original = regular_bytes(target)
    closed = True
    try:
        yield
    except ClosureFailure:
        closed = False
        raise
    finally:
        if closed:
            target.write_bytes(original)


def command(
    arguments: list[str],
    root: Path,
    deadline: float,
    stop: Stop,
    *,
    limit: int = MAX_OUTPUT,
    variables: dict[str, str] | None = None,
    workspace: Path | None = None,
) -> bytes:
    result = execute(
        arguments,
        root,
        min(deadline, time.monotonic() + 60),
        stop,
        variables=variables,
        limit=limit,
        workspace=workspace,
    )
    if result.reason != "completed" or result.code != 0:
        raise CommandFailure(result, arguments[0])
    return result.output


def unpack(content: bytes, root: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        total = 0
        for index, member in enumerate(archive):
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or index >= MAX_MUTANTS:
                raise ValueError("invalid archive path")
            if member.isdir():
                (root / path).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or not 0 <= member.size <= MAX_FILE:
                raise ValueError("invalid archive entry")
            total += member.size
            if total > MAX_WORK:
                raise ValueError("oversized source archive")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("missing archive entry")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(MAX_FILE + 1))


def clean_bytecode(root: Path) -> None:
    for base in ("src", "tests"):
        for path in (root / base).rglob("*.pyc"):
            path.unlink()


def classify(result: Execution, junit: Path) -> Outcome:
    if result.reason != "completed":
        reasons: dict[str, Outcome] = {
            "timeout": "timeout",
            "interrupted": "interrupted",
            "output_limit": "output_limit",
            "workspace_limit": "workspace_limit",
        }
        return reasons.get(result.reason, "infrastructure_error")
    if result.code not in {0, 1} or not junit.is_file():
        return "test_error"
    counts: Counter[str] = Counter()

    def start(name: str, attributes: dict[str, str]) -> None:
        if not counts and name != "testsuites":
            raise ValueError("invalid junit root")
        counts[name] += 1

    def doctype(name: str, system: str | None, public: str | None, subset: int) -> None:
        raise ValueError("junit document types are forbidden")

    try:
        parser = expat.ParserCreate()
        parser.StartElementHandler = start
        parser.StartDoctypeDeclHandler = doctype
        parser.Parse(regular_bytes(junit), True)
    except (expat.ExpatError, ValueError):
        return "test_error"
    if not counts["testcase"] or counts["error"] or counts["skipped"]:
        return "test_error"
    failures = counts["failure"] > 0
    if result.code == 1 and failures:
        return "killed"
    return "survived" if result.code == 0 and not failures else "test_error"


def test_scope(
    root: Path,
    scope: Scope,
    deadline: float,
    stop: Stop,
    seconds: int,
) -> Outcome:
    clean_bytecode(root)
    junit = root / "audit-junit.xml"
    junit.unlink(missing_ok=True)
    result = execute(
        [
            str(root / ".venv/bin/python"),
            "-B",
            "-m",
            "pytest",
            "-q",
            "-x",
            "--tb=no",
            "--junitxml=audit-junit.xml",
            scope.tests,
        ],
        root,
        min(deadline, time.monotonic() + seconds),
        stop,
        workspace=root,
    )
    return classify(result, junit)


def mutation_order(item: Mutation) -> tuple[str, tuple[int, int], str, int]:
    return item.module_path, item.start_pos, item.operator_name, item.occurrence


def inventory(root: Path, tool: Path, deadline: float, stop: Stop) -> list[Mutation]:
    targets = json.dumps([scope.source for scope in SCOPES])
    config = root / "audit.toml"
    config.write_text(
        f"[cosmic-ray]\nmodule-path = {targets}\ntimeout = 30\n"
        'excluded-modules = []\ntest-command = "unused"\n'
        '[cosmic-ray.distributor]\nname = "local"\n',
        encoding="utf-8",
    )
    command(
        [str(tool), "init", str(config), "audit.sqlite"],
        root,
        deadline,
        stop,
        workspace=root,
    )
    data = command(
        [str(tool), "dump", "audit.sqlite"], root, deadline, stop, workspace=root
    )
    mutations: list[Mutation] = []
    duplicates: Counter[str] = Counter()
    lines = data.splitlines()
    if not 0 < len(lines) <= MAX_MUTANTS:
        raise ValueError("invalid mutation inventory size")
    native = [WORK.validate_json(line)[0].mutations[0] for line in lines]
    for mutation in sorted(native, key=mutation_order):
        if mutation.module_path not in {scope.source for scope in SCOPES}:
            raise ValueError("mutation outside manifest")
        source = regular_bytes(root / mutation.module_path).decode().splitlines()
        context = source[mutation.start_pos[0] - 1].strip()
        key = (
            f"{mutation.module_path}:{mutation.definition_name}:"
            f"{mutation.operator_name}:{context}"
        )
        position = duplicates[key]
        duplicates[key] += 1
        fingerprint = hashlib.sha256(f"{key}:{position}".encode()).hexdigest()
        mutations.append(mutation.model_copy(update={"fingerprint": fingerprint}))
        if len(mutations) > MAX_MUTANTS:
            raise ValueError("oversized mutation inventory")
    if not mutations:
        raise ValueError("empty mutation inventory")
    regular_bytes(root / "audit.sqlite")
    return mutations


def save(report: Report, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    content = report.model_dump_json()
    if len(content.encode()) > MAX_FILE:
        raise ValueError("oversized audit report")
    (output / "report.json").write_text(content, encoding="utf-8")
    counts = Counter(value.outcome for value in report.results.values())
    executed = sum(
        value.outcome != "pending" and not value.reused
        for value in report.results.values()
    )
    lines = [
        "# Mutation audit",
        "",
        f"Source: `{report.source_revision}`",
        f"Tooling: `{report.tooling_revision}`",
        f"Status: {report.status} ({report.reason})",
        f"Continuation: {report.continuation}",
        f"Reused: {sum(value.reused for value in report.results.values())}",
        f"Executed: {executed}",
        "",
        "| Outcome | Count |",
        "|---|---:|",
        *(f"| {name} | {count} |" for name, count in sorted(counts.items())),
        "",
        "Only the declared pricing and CEL-path scopes are audited.",
        "",
        "| Finding | Source | Definition | Outcome |",
        "|---|---|---|---|",
    ]
    groups: set[tuple[str, str | None, str]] = set()
    for value in report.results.values():
        mutation = value.mutation
        group = (mutation.module_path, mutation.definition_name, value.outcome)
        if value.outcome in {"killed", "pending"} or group in groups:
            continue
        groups.add(group)
        identity = hashlib.sha256(str(group).encode()).hexdigest()[:12]
        lines.append(f"| {identity} | {group[0]} | {group[1]} | {group[2]} |")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def audit(
    source: Path,
    revision: str,
    output: Path,
    stop: Stop,
    started: float,
    seconds: int = 480,
    maximum: int = MAX_MUTANTS,
    *,
    offline: bool = False,
    previous: Report | None = None,
) -> Report:
    if not math.isfinite(started) or not 1 <= seconds <= 480:
        raise ValueError("invalid audit budget")
    deadline = time.monotonic() + min(seconds, 480) - max(0, time.time() - started)
    git = shutil.which("git")
    uv = shutil.which("uv")
    if git is None or uv is None:
        raise FileNotFoundError("audit requires git and uv")
    tooling = command([git, "rev-parse", "HEAD"], ROOT, deadline, stop).decode().strip()
    report = Report(source_revision=revision, tooling_revision=tooling)
    report = report.model_copy(
        update={
            "tooling_dirty": bool(
                command(
                    [git, "status", "--porcelain"],
                    ROOT,
                    deadline,
                    stop,
                )
            )
        }
    )
    event = os.environ.get("GITHUB_EVENT_NAME", "local")
    if event != "local":
        if (
            event not in {"schedule", "workflow_dispatch"}
            or os.environ.get("GITHUB_REF") != "refs/heads/master"
            or os.environ.get("GITHUB_SHA") != revision
            or command([git, "rev-parse", "HEAD"], source, deadline, stop)
            .decode()
            .strip()
            != revision
        ):
            raise ValueError("mutation audit requires exact native master revision")
        report = report.model_copy(
            update={
                "event": event,
                "run_id": int(os.environ["GITHUB_RUN_ID"]),
                "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            }
        )
        variables = environment(ROOT)
        for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_TOKEN"):
            variables[name] = os.environ[name]
        remote = execute(
            [
                sys.executable,
                "-B",
                "-m",
                "scripts.ci.mutation_remote",
            ],
            ROOT,
            min(deadline, time.monotonic() + 30),
            stop,
            variables=variables,
            limit=MAX_FILE,
        )
        if remote.reason == "completed" and remote.code == 0:
            if remote.output != b"null":
                previous = Report.model_validate_json(remote.output)
                report = report.model_copy(update={"continuation": "restored"})
        else:
            report = report.model_copy(update={"continuation": "unavailable"})
    else:
        command(
            [git, "merge-base", "--is-ancestor", revision, "origin/master"],
            source,
            deadline,
            stop,
        )
    try:
        with workspace(output) as root:
            content = command(
                [
                    git,
                    "archive",
                    revision,
                    "src",
                    "tests",
                    "pyproject.toml",
                    "uv.lock",
                    "hatch_build.py",
                    "README.md",
                    "LICENSE",
                    ".python-version",
                ],
                source,
                deadline,
                stop,
                limit=MAX_WORK,
            )
            unpack(content, root)
            variables = environment(root)
            variables["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
            cache = (
                command(
                    [uv, "cache", "dir"],
                    ROOT,
                    deadline,
                    stop,
                    variables={**environment(ROOT), "HOME": str(Path.home())},
                )
                .decode()
                .strip()
            )
            variables["UV_CACHE_DIR"] = cache
            sync = [
                uv,
                "sync",
                "--python",
                sys.executable,
                "--frozen",
                "--project",
                str(ROOT),
                "--group",
                "mutation",
                "--group",
                "build",
            ]
            if offline:
                sync.append("--offline")
            bootstrap = sync[: sync.index("--group")] + ["--group", "build"]
            if offline:
                bootstrap.append("--offline")
            command(
                [*bootstrap, "--no-build", "--no-install-project"],
                root,
                deadline,
                stop,
                variables=variables,
            )
            command(
                [*sync, "--no-build-isolation"],
                root,
                deadline,
                stop,
                variables=variables,
            )
            command(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(root / ".venv/bin/python"),
                    "--no-deps",
                    "--no-build-isolation",
                    "--editable",
                    str(root),
                ],
                root,
                deadline,
                stop,
                variables=variables,
            )
            probe = (
                "import sys; from pathlib import Path; import kora.ir.pricing as p; "
                "import kora.cel.paths as c; "
                "assert all(Path(m.__file__).resolve().is_relative_to(Path.cwd()) "
                "for m in (p,c))"
            )
            command(
                [str(root / ".venv/bin/python"), "-I", "-B", "-c", probe],
                root,
                deadline,
                stop,
            )
            if workspace_bytes(root / ".venv") > 256 * 1024 * 1024:
                raise ValueError("oversized dependency environment")
            for scope in SCOPES:
                if test_scope(root, scope, deadline, stop, 60) != "survived":
                    raise RuntimeError("audit baseline did not pass")
                output.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(
                    root / "audit-junit.xml",
                    output / f"{Path(scope.source).stem}-baseline.xml",
                )
            tool = root / ".venv/bin/cosmic-ray"
            version = command([str(tool), "--version"], root, deadline, stop)
            if version.strip() != b"cosmic-ray, version 8.7.0":
                raise ValueError("mutation tool identity mismatch")
            work = inventory(root, tool, deadline, stop)
            shutil.copyfile(root / "audit.sqlite", output / "inventory.sqlite")
            inputs = {
                scope.source: digest(root, scope) + digest(ROOT, scope)
                for scope in SCOPES
            }
            values = {
                item.identity(): Result(
                    mutation=item,
                    outcome="pending",
                    input_digest=inputs[item.module_path],
                    tested_revision=revision,
                )
                for item in work
            }
            generation = previous.generation + 1 if previous else 0
            if previous and generation % 7:
                for key, value in previous.results.items():
                    if (
                        key in values
                        and value.input_digest == values[key].input_digest
                        and value.mutation == values[key].mutation
                        and value.outcome in {"killed", "survived"}
                    ):
                        values[key] = value.model_copy(update={"reused": True})
            remaining = dict.fromkeys(
                item.identity() for item in rotation_order(work, previous)
            )
            for key, value in values.items():
                if value.outcome != "pending":
                    remaining.pop(key, None)
            report = report.model_copy(
                update={"results": values, "generation": generation}
            )
            for index, item in enumerate(ordered(work, previous)):
                if (
                    stop.requested
                    or time.monotonic() + 5 >= deadline
                    or index >= maximum
                ):
                    break
                key = item.identity()
                if values[key].outcome != "pending":
                    continue
                target = root / item.module_path
                with restored_source(target):
                    command(
                        [
                            str(tool),
                            "apply",
                            item.module_path,
                            item.operator_name,
                            str(item.occurrence),
                        ],
                        root,
                        deadline,
                        stop,
                        workspace=root,
                    )
                    try:
                        ast.parse(regular_bytes(target))
                    except SyntaxError:
                        outcome: Outcome = "invalid"
                    else:
                        scope = next(
                            scope
                            for scope in SCOPES
                            if scope.source == item.module_path
                        )
                        outcome = test_scope(root, scope, deadline, stop, 30)
                values[key] = Result(
                    mutation=item,
                    outcome=outcome,
                    input_digest=inputs[item.module_path],
                    tested_revision=revision,
                )
                remaining.pop(key, None)
            completed = all(
                value.outcome in {"killed", "survived", "invalid"}
                for value in values.values()
            )
            report = report.model_copy(
                update={
                    "results": values,
                    "rotation": tuple(remaining),
                    "next_identity": next(iter(remaining), ""),
                    "status": "complete" if completed else "partial",
                    "reason": "finished"
                    if completed
                    else "interrupted"
                    if stop.requested
                    else report.reason,
                }
            )
    except (OSError, ValueError, RuntimeError) as error:
        report = report.model_copy(
            update={
                "status": "failed",
                "reason": error.reason
                if isinstance(error, CommandFailure)
                else "execution_failed",
                "retained_workspace": isinstance(error, ClosureFailure),
            }
        )
    finally:
        report = report.model_copy(update={"seconds": max(0, time.time() - started)})
        save(report, output)
    return report


def run(source: Path = ROOT) -> int:
    parser = Parser()
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, default=Path("mutation-report"))
    parser.add_argument("--started", type=float, default=time.time())
    parser.add_argument("--seconds", type=int, choices=range(1, 481), default=480)
    parser.add_argument(
        "--maximum", type=int, choices=range(1, MAX_MUTANTS + 1), default=MAX_MUTANTS
    )
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    stop = Stop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop.receive)
    report = audit(
        source,
        arguments.source_revision,
        arguments.output,
        stop,
        arguments.started,
        arguments.seconds,
        arguments.maximum,
        offline=arguments.offline,
    )
    sys.stderr.write(
        json.dumps(
            {
                "event": "mutation.audit.completed",
                "level": "ERROR" if report.status == "failed" else "INFO",
                "status": report.status,
                "reason": report.reason,
                "seconds": report.seconds,
            }
        )
        + "\n",
    )
    return int(report.status == "failed")


def main(source: Path = ROOT) -> int:
    try:
        return run(source)
    except (OSError, ValueError, RuntimeError):
        sys.stderr.write(
            '{"event":"mutation.audit.failed","level":"ERROR",'
            '"reason":"startup_or_publication_failed"}\n'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
