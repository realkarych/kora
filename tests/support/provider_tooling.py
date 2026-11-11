import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from packaging.requirements import Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from kora.ir.json import JsonObject
from scripts.ci.mutation_process import ClosureFailure, Stop, execute
from tests.support.observability import ProcessResult

REPOSITORY: Path = Path(__file__).resolve().parents[2]
FIXTURE: Path = REPOSITORY / "tests" / "fixtures" / "provider_tooling"
CORE_WHEEL_BUILD_SECONDS = 120


class ProjectDependencies(BaseModel):
    dependencies: list[str]
    optional_dependencies: dict[str, list[str]] = Field(alias="optional-dependencies")


class ProjectManifest(BaseModel):
    project: ProjectDependencies


class LockedPackage(BaseModel):
    name: str
    version: str


class DependencyLock(BaseModel):
    package: list[LockedPackage]


def run_setup(
    arguments: list[str], *, environment: Mapping[str, str] | None = None
) -> None:
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        env=None if environment is None else dict(environment),
        timeout=120,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace"))


def locked_payload_wheel(
    distribution: importlib.metadata.Distribution, output: Path
) -> None:
    files = distribution.files
    if files is None:
        raise ValueError("installed dependency has no payload record")
    wheel = distribution.read_text("WHEEL")
    if wheel is None:
        raise ValueError("installed dependency is not a wheel")
    compatible = frozenset(sys_tags())
    tag = next(
        line.removeprefix("Tag: ")
        for line in wheel.splitlines()
        if line.startswith("Tag: ")
        and parse_tag(line.removeprefix("Tag: ")) & compatible
    )
    name = canonicalize_name(distribution.metadata["Name"]).replace("-", "_")
    target = output / f"{name}-{distribution.version}-{tag}.whl"
    records: list[tuple[str, str, str]] = []
    record_path: str | None = None
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in files:
            if ".." in entry.parts or entry.is_absolute() or entry.suffix == ".pyc":
                continue
            if entry.name == "RECORD":
                record_path = entry.as_posix()
                continue
            if entry.name in {"INSTALLER", "REQUESTED", "direct_url.json"}:
                continue
            payload = Path(str(distribution.locate_file(entry))).read_bytes()
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                entry.hash is None
                or entry.hash.mode != "sha256"
                or entry.hash.value != digest
                or entry.size != len(payload)
            ):
                raise ValueError(
                    f"installed payload differs from its record: {name}/{entry}"
                )
            archive.writestr(entry.as_posix(), payload)
            records.append((entry.as_posix(), f"sha256={digest}", str(len(payload))))
        if record_path is None:
            raise ValueError("installed dependency has no RECORD")
        records.append((record_path, "", ""))
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(records)
        archive.writestr(record_path, buffer.getvalue())


def prepare_wheelhouse(
    output: Path, *, conformance: bool = False, postgres: bool = False
) -> None:
    configuration = ProjectManifest.model_validate(
        tomllib.loads((REPOSITORY / "pyproject.toml").read_text())
    )
    lock = DependencyLock.model_validate(
        tomllib.loads((REPOSITORY / "uv.lock").read_text())
    )
    locked = {(canonicalize_name(item.name), item.version) for item in lock.package}
    requirements = list(configuration.project.dependencies)
    if conformance:
        requirements += configuration.project.optional_dependencies["conformance"]
    if postgres:
        requirements += configuration.project.optional_dependencies["postgres"]
    pending = [Requirement(value) for value in requirements]
    seen: dict[str, frozenset[str]] = {}
    while pending:
        requested = pending.pop()
        name = canonicalize_name(requested.name)
        extras = frozenset(requested.extras) | seen.get(name, frozenset())
        if name in seen and seen[name] == extras:
            continue
        distribution = importlib.metadata.distribution(name)
        if (
            name,
            distribution.version,
        ) not in locked or distribution.version not in requested.specifier:
            raise ValueError("installed dependency does not match the lock")
        if name not in seen:
            locked_payload_wheel(distribution, output)
        seen[name] = extras
        for value in distribution.requires or ():
            requirement = Requirement(value)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in {"", *extras}
            ):
                pending.append(requirement)


def build_wheel(
    source: Path, output: Path, *, environment: Mapping[str, str] | None = None
) -> None:
    run_setup(
        [
            "uv",
            "build",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--wheel",
            "--out-dir",
            str(output),
            str(source),
        ],
        environment=environment,
    )


def create_virtual_environment(directory: Path) -> Path:
    run_setup(
        [
            "uv",
            "venv",
            "--offline",
            "--no-project",
            "--python",
            sys.executable,
            str(directory),
        ]
    )
    return directory / "bin" / "python"


@contextmanager
def core_wheel_workspace() -> Generator[Path]:
    root = Path(tempfile.mkdtemp(prefix="kora-core-wheel-"))
    settled = True
    try:
        yield root
    except ClosureFailure:
        settled = False
        raise
    finally:
        if settled:
            shutil.rmtree(root)


@contextmanager
def core_wheel_build_stop() -> Generator[Stop]:
    stop = Stop()
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = [(number, signal.getsignal(number)) for number in signals]
    if any(handler is None for _, handler in previous):
        raise RuntimeError("core wheel build requires restorable signal handlers")
    try:
        for number in signals:
            signal.signal(number, stop.receive)
        yield stop
    finally:
        for number, handler in previous:
            signal.signal(number, handler)


def require_core_wheel_build_current(deadline: float, stop: Stop) -> None:
    if stop.requested:
        raise KeyboardInterrupt
    if time.monotonic() >= deadline:
        raise TimeoutError("core wheel build exceeded its deadline")


def run_core_wheel_build(
    arguments: list[str],
    root: Path,
    variables: dict[str, str],
    deadline: float,
    stop: Stop,
) -> None:
    result = execute(
        arguments,
        root,
        deadline,
        stop,
        variables=variables,
        limit=None,
        stderr_only=True,
    )
    require_core_wheel_build_current(deadline, stop)
    if result.reason != "completed" or result.code != 0:
        stderr = result.output.decode(errors="replace")
        raise ValueError(
            f"core wheel build failed ({result.reason}, {result.code})\n{stderr}"
        )


def core_wheel_artifact(output: Path, pattern: str) -> Path:
    artifacts = tuple(output.glob(pattern))
    if len(artifacts) != 1 or artifacts[0].is_symlink() or not artifacts[0].is_file():
        raise ValueError("core wheel build did not produce one regular artifact")
    return artifacts[0]


def build_core_wheel(output: Path) -> Path:
    deadline = time.monotonic() + CORE_WHEEL_BUILD_SECONDS
    with core_wheel_build_stop() as stop:
        temporary = output / "temporary"
        temporary.mkdir()
        variables = {**os.environ, "KORA_BUILD_OFFLINE": "1", "TMPDIR": str(temporary)}
        arguments = [
            "uv",
            "build",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
        ]
        run_core_wheel_build(
            [*arguments, "--sdist", str(REPOSITORY)], output, variables, deadline, stop
        )
        archive = core_wheel_artifact(output, "kora-*.tar.gz")
        run_core_wheel_build(
            [*arguments, "--wheel", str(archive)], output, variables, deadline, stop
        )
        wheel = core_wheel_artifact(output, "kora-*.whl")
        require_core_wheel_build_current(deadline, stop)
    require_core_wheel_build_current(deadline, stop)
    return wheel


def copy_core_wheel(core_wheel: Path, wheelhouse: Path) -> Path:
    target = wheelhouse / core_wheel.name
    shutil.copyfile(core_wheel, target)
    return target


def build_fixture_wheel(
    root: Path,
    entries: Mapping[str, str],
    control: JsonObject | None = None,
    *,
    inventory: int = 0,
) -> Path:
    source = root / "fixture"
    shutil.copytree(FIXTURE, source)
    if inventory:
        module = "from kora.protocols.metadata import ProviderMetadata\n"
        module += "from kora_tooling_fixture.measurement import measured_metadata\n\n"
        module += "\n".join(
            f"def item_{index:03}() -> ProviderMetadata:\n"
            f'    return measured_metadata("item_{index:03}")\n'
            for index in range(inventory)
        )
        (source / "src" / "kora_tooling_fixture" / "inventory.py").write_text(module)
    if control is not None:
        (source / "src" / "kora_tooling_fixture" / "control.json").write_text(
            json.dumps(control)
        )
    project = (source / "project.toml").read_text()
    project += '\n[project.entry-points."kora.provider"]\n'
    project += "\n".join(
        f"{json.dumps(name)} = {json.dumps(value)}"
        for name, value in sorted(entries.items())
    )
    (source / "pyproject.toml").write_text(project)
    output = root / "wheels"
    output.mkdir()
    build_wheel(source, output)
    return next(output.glob("*.whl"))


def install_measurement_fixture(root: Path, fixture_wheel: Path) -> Path:
    python = create_virtual_environment(root / "measurement-environment")
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import os, sysconfig; os.write(1, sysconfig.get_path('purelib').encode())",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    packages = Path(result.stdout.decode().strip())
    (packages / "kora-study-base.pth").write_text(
        str(REPOSITORY / "src") + "\n" + sysconfig.get_path("purelib") + "\n"
    )
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--no-deps",
            "--python",
            str(python),
            str(fixture_wheel),
        ]
    )
    return python


INSTALLED_COMMAND_SECONDS: Final[int] = 60


@dataclass(frozen=True)
class InstalledTooling:
    python: Path
    outside: Path

    def command(self, *arguments: str) -> list[str]:
        return [str(self.python), "-I", "-m", "kora", *arguments]

    def environment(self, environment: Mapping[str, str] | None) -> dict[str, str]:
        return {"PATH": os.defpath, **({} if environment is None else environment)}

    def run(
        self,
        *arguments: str,
        environment: Mapping[str, str] | None = None,
        umask: int = -1,
    ) -> ProcessResult:
        completed = subprocess.run(
            self.command(*arguments),
            cwd=self.outside,
            env=self.environment(environment),
            capture_output=True,
            check=False,
            timeout=INSTALLED_COMMAND_SECONDS,
            umask=umask,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def install_tooling(
    root: Path,
    wheelhouse: Path,
    fixture_wheel: Path,
    *,
    conformance: bool = False,
    postgres: bool = False,
) -> InstalledTooling:
    python = create_virtual_environment(root / "installed")
    extras = [
        name
        for name, selected in (("conformance", conformance), ("postgres", postgres))
        if selected
    ]
    requirement = "kora[" + ",".join(extras) + "]" if extras else "kora"
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--python",
            str(python),
            requirement,
            str(fixture_wheel),
        ]
    )
    outside = root / "outside"
    outside.mkdir()
    return InstalledTooling(python, outside)
