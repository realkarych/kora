import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from kora.ir.json import JsonObject
from tests.support.observability import ProcessResult

REPOSITORY: Path = Path(__file__).resolve().parents[2]
FIXTURE: Path = REPOSITORY / "tests" / "fixtures" / "provider_tooling"


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


def prepare_wheelhouse(output: Path, *, conformance: bool = False) -> None:
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


def build_core_wheel(output: Path) -> Path:
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
            str(REPOSITORY),
        ],
        environment={**os.environ, "KORA_BUILD_OFFLINE": "1"},
    )
    return next(output.glob("kora-*.whl"))


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
        ]
    )
    return next(output.glob("*.whl"))


def install_measurement_fixture(root: Path, fixture_wheel: Path) -> Path:
    environment = root / "measurement-environment"
    run_setup(
        [
            "uv",
            "venv",
            "--offline",
            "--no-project",
            "--python",
            sys.executable,
            str(environment),
        ]
    )
    python = environment / "bin" / "python"
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


@dataclass(frozen=True)
class InstalledTooling:
    python: Path
    outside: Path

    def run(
        self, *arguments: str, environment: Mapping[str, str] | None = None
    ) -> ProcessResult:
        completed = subprocess.run(
            [str(self.python), "-I", "-m", "kora", *arguments],
            cwd=self.outside,
            env={"PATH": os.defpath, **({} if environment is None else environment)},
            capture_output=True,
            check=False,
            timeout=60,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def install_tooling(
    root: Path, wheelhouse: Path, fixture_wheel: Path, *, conformance: bool = False
) -> InstalledTooling:
    environment = root / "installed"
    run_setup(
        [
            "uv",
            "venv",
            "--offline",
            "--no-project",
            "--python",
            sys.executable,
            str(environment),
        ]
    )
    python = environment / "bin" / "python"
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
            "kora[conformance]" if conformance else "kora",
            str(fixture_wheel),
        ]
    )
    outside = root / "outside"
    outside.mkdir()
    return InstalledTooling(python, outside)
