import tomllib
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def project_metadata(repository_root: Path) -> tuple[str, str]:
    project: dict[str, object] = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    name = project["name"]
    version = project["version"]
    assert isinstance(name, str)
    assert isinstance(version, str)
    return name, version


@pytest.fixture(scope="session")
def distributions(
    repository_root: Path, project_metadata: tuple[str, str]
) -> tuple[Path, Path]:
    artifact_directory = repository_root / "dist"
    wheels = sorted(artifact_directory.glob("*.whl"))
    source_archives = sorted(artifact_directory.glob("*.tar.gz"))
    assert len(wheels) == 1, f"Build exactly one wheel before pytest: {wheels}"
    assert len(source_archives) == 1, (
        f"Build exactly one source distribution before pytest: {source_archives}"
    )
    name, version = project_metadata
    wheel = wheels[0]
    source_archive = source_archives[0]
    assert wheel.name == f"{name}-{version}-py3-none-any.whl"
    assert source_archive.name == f"{name}-{version}.tar.gz"
    return wheel, source_archive
