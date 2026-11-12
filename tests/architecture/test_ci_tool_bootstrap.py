import os
import platform
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE_SETUP = ROOT / "scripts/ci/setup_scaffold_node.sh"
UV_SETUP = ROOT / "scripts/ci/setup_uv_cache.sh"
LINK_CHECK = ROOT / "scripts/ci/check_links.sh"


@pytest.mark.parametrize(
    "cache_state", ["directory", "marker", "destination_symlink", "binary_symlink"]
)
def test_scaffold_node_refuses_partial_or_symlink_cache(
    tmp_path: Path, cache_state: str
) -> None:
    version = (ROOT / "web/.node-version").read_text().strip()
    machine = (platform.system(), platform.machine())
    architecture = {("Darwin", "arm64"): "arm64", ("Linux", "x86_64"): "x64"}.get(
        machine
    )
    cache_root = tmp_path / "tool cache"
    version_root = cache_root / "node" / version
    version_root.mkdir(parents=True)
    if architecture is not None:
        destination = version_root / architecture
        marker = version_root / f"{architecture}.complete"
        if cache_state in {"directory", "binary_symlink"}:
            (destination / "bin").mkdir(parents=True)
        if cache_state != "directory":
            marker.touch()
        if cache_state == "destination_symlink":
            target = tmp_path / "real directory"
            (target / "bin").mkdir(parents=True)
            binary = target / "bin/node"
            binary.touch(mode=0o700)
            destination.symlink_to(target, target_is_directory=True)
        if cache_state == "binary_symlink":
            (destination / "bin/node").symlink_to(Path("/bin/bash").resolve())
    before = sorted(cache_root.rglob("*"))

    result = subprocess.run(
        ("/bin/bash", str(NODE_SETUP)),
        cwd=ROOT,
        env={**os.environ, "RUNNER_TOOL_CACHE": str(cache_root.resolve())},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    expected = (
        b"Incomplete or invalid Scaffold Node cache entry"
        if architecture is not None
        else b"Unsupported Scaffold Node archive platform"
    )
    assert expected in result.stderr
    assert result.stdout == b""
    assert sorted(cache_root.rglob("*")) == before


@pytest.mark.parametrize(
    "cache_state", ["directory", "marker", "destination_symlink", "binary_symlink"]
)
def test_uv_setup_refuses_partial_or_symlink_cache(
    tmp_path: Path, cache_state: str
) -> None:
    machine = (platform.system(), platform.machine())
    architecture = {("Darwin", "arm64"): "aarch64", ("Linux", "x86_64"): "x86_64"}.get(
        machine
    )
    cache_root = tmp_path / "tool cache"
    version_root = cache_root / "uv" / "0.13.1"
    version_root.mkdir(parents=True)
    if architecture is not None:
        destination = version_root / architecture
        if cache_state in {"directory", "binary_symlink"}:
            destination.mkdir()
        if cache_state != "directory":
            (version_root / f"{architecture}.complete").touch()
        if cache_state == "destination_symlink":
            target = tmp_path / "real directory"
            target.mkdir()
            for name in ("uv", "uvx"):
                (target / name).touch(mode=0o700)
            destination.symlink_to(target, target_is_directory=True)
        if cache_state == "binary_symlink":
            (destination / "uv").symlink_to(Path("/bin/bash").resolve())
            (destination / "uvx").touch(mode=0o700)
    before = sorted(cache_root.rglob("*"))

    result = subprocess.run(
        ("/bin/bash", str(UV_SETUP), "3.11.17"),
        cwd=ROOT,
        env={**os.environ, "RUNNER_TOOL_CACHE": str(cache_root.resolve())},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    expected = (
        b"Incomplete or invalid uv tool cache entry"
        if architecture is not None
        else b"Unsupported uv archive platform"
    )
    assert expected in result.stderr
    assert result.stdout == b""
    assert sorted(cache_root.rglob("*")) == before


def test_scaffold_node_requires_the_recorded_version(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / ".node-version").write_text("0.0.0\n")

    result = subprocess.run(
        ("/bin/bash", str(NODE_SETUP)),
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert b"must match web/.node-version" in result.stderr
    assert result.stdout == b""


def test_link_path_arguments_are_literal_local_and_include_selected_hidden_files(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "paths"
    manifest.write_bytes(
        b"https:local.md\0--help.md\0bad[a]*?.md\0a file.md\0two\nlines.md\0"
        b".hidden[?].md\0"
    )
    report = tmp_path / "report.md"
    command = 'source "$1"; link_arguments "$2" "$3"; printf "%s\\0" "${link_args[@]}"'

    result = subprocess.run(
        (
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(LINK_CHECK),
            str(manifest),
            str(report),
        ),
        cwd=tmp_path,
        capture_output=True,
        check=True,
        timeout=5,
    )

    assert result.stdout == (
        b"--hidden\0--mode\0task\0--format\0markdown\0--output\0"
        + os.fsencode(report)
        + b"\0--config\0lychee.toml\0--\0"
        b"./https:local.md\0./--help.md\0./bad[[]a[]][*][?].md\0"
        b"./a file.md\0./two\nlines.md\0./.hidden[[][?][]].md\0"
    )


def test_link_arguments_refuse_a_selected_alias_to_a_directory(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".unselected.md").write_text("[unselected](./absent.md)\n")
    (repository / "alias.md").symlink_to(outside, target_is_directory=True)
    manifest = tmp_path / "paths"
    manifest.write_bytes(b"ordinary.md\0alias.md\0")
    command = 'source "$1"; link_arguments "$2" "$3"; printf "%s\\0" "${link_args[@]}"'

    result = subprocess.run(
        (
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(LINK_CHECK),
            str(manifest),
            str(tmp_path / "report.md"),
        ),
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stderr == b"A selected Markdown input resolves to a directory\n"
    assert result.stdout == b""


@pytest.mark.parametrize(
    ("report", "accepted"),
    [
        (None, False),
        ("", False),
        ("| 🔍 Total | 12 |\n", False),
        ("# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | 0 |\n", False),
        ("# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | no |\n", False),
        ("# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | 12 |\n", True),
        (
            "# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | 12 |\n"
            "| 🔍 Total | 8 |\n",
            False,
        ),
        (
            "# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | 12 |\n"
            "\n## File content\n| 🔍 Total | 0 |\n",
            True,
        ),
        (
            "# Summary\n\n| Status | Count |\n|----|----|\n| 🔍 Total | 0 |\n"
            "\n## File content\n| 🔍 Total | 12 |\n",
            False,
        ),
    ],
)
def test_link_report_requires_one_positive_total(
    tmp_path: Path, report: str | None, accepted: bool
) -> None:
    path = tmp_path / "lychee.md"
    if report is not None:
        path.write_text(report, encoding="utf-8")
    command = 'source "$1"; link_report_has_positive_total "$2"'

    result = subprocess.run(
        ("/bin/bash", "-c", command, "bash", str(LINK_CHECK), str(path)),
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is accepted


def test_link_console_suspends_workflow_commands_during_checker_output(
    tmp_path: Path,
) -> None:
    checker_log = tmp_path / "checker.log"
    checker_log.write_bytes(b"::error::untrusted\r::warning::untrusted\n")
    report = tmp_path / "lychee.md"
    report.write_bytes(b"## proof##[warning]untrusted.md")
    command = 'source "$1"; print_lychee_output "$2" "$3"'

    result = subprocess.run(
        (
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(LINK_CHECK),
            str(checker_log),
            str(report),
        ),
        capture_output=True,
        check=True,
        timeout=5,
    )

    assert re.fullmatch(
        rb"::stop-commands::([0-9a-f]{64})\n"
        rb"::error::untrusted\r::warning::untrusted\n"
        rb"## proof##\[warning\]untrusted\.md\n::\1::\n",
        result.stdout,
    )
    assert result.stderr == b""
