import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def test_source_distribution_contains_rebuild_inputs(
    distributions: tuple[Path, Path], project_metadata: tuple[str, str]
) -> None:
    _, source_archive = distributions
    name, version = project_metadata
    root = f"{name}-{version}"
    with tarfile.open(source_archive) as archive:
        members = archive.getmembers()
        entries = {PurePosixPath(member.name) for member in members}
        assert all(path.parts[0] == root for path in entries)
        paths = {path.relative_to(root) for path in entries}
        file_paths = {
            PurePosixPath(member.name).relative_to(root)
            for member in members
            if member.isfile()
        }
    required = {
        ".python-version",
        "LICENSE",
        "README.md",
        "hatch_build.py",
        "pyproject.toml",
        "uv.lock",
        "src/kora/__init__.py",
        "src/kora/py.typed",
        "web/.node-version",
        "web/package.json",
        "web/pnpm-lock.yaml",
        "web/pnpm-workspace.yaml",
        "web/index.html",
        "web/tsconfig.json",
        "web/tsconfig.app.json",
        "web/tsconfig.node.json",
        "web/vite.config.ts",
        "web/eslint.config.mjs",
        "web/src/main.tsx",
        "web/src/App.tsx",
        "web/src/index.css",
    }
    assert {PurePosixPath(path) for path in required} <= file_paths
    forbidden_parts = {
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".vite",
        "dist",
        "build",
        "site-packages",
    }
    assert not {path for path in paths if forbidden_parts.intersection(path.parts)}
    assert not {path for path in paths if path.is_relative_to("src/kora/api/static")}


def test_installed_wheel_contains_web_assets(
    distributions: tuple[Path, Path],
    project_metadata: tuple[str, str],
    repository_root: Path,
    tmp_path: Path,
) -> None:
    wheel, _ = distributions
    name, version = project_metadata
    with ZipFile(wheel) as archive:
        paths = {PurePosixPath(entry) for entry in archive.namelist()}
    expected_roots = {name, f"{name}-{version}.dist-info"}
    assert {path.parts[0] for path in paths} == expected_roots
    assert not {
        path
        for path in paths
        if {"node_modules", "web", ".venv", ".vite"}.intersection(path.parts)
        or path.suffix in {".ts", ".tsx", ".jsx", ".map"}
        or path.name in {"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"}
    }
    environment = tmp_path / "installed"
    working_directory = tmp_path / "working"
    working_directory.mkdir()
    assert not tmp_path.resolve().is_relative_to(repository_root.resolve())
    subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable, str(environment)],
        cwd=working_directory,
        check=True,
    )
    interpreter = environment / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--no-deps",
            "--python",
            str(interpreter),
            str(wheel),
        ],
        cwd=working_directory,
        check=True,
    )
    subprocess.run(
        [
            str(interpreter),
            "-I",
            str(repository_root / "tests" / "packaging" / "inspect_install.py"),
            name,
            version,
            str(environment),
            str(sys.version_info.major),
            str(sys.version_info.minor),
            str(repository_root / "LICENSE"),
        ],
        cwd=working_directory,
        check=True,
    )
