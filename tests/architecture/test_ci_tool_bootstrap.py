import os
import platform
import re
import subprocess
from pathlib import Path

import pytest

from tests.support.workflows import action_name, step_list, table, workflow_jobs

ROOT = Path(__file__).resolve().parents[2]
NODE_SETUP = ROOT / "scripts/ci/setup_scaffold_node.sh"
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


def test_link_path_arguments_are_literal_and_local(tmp_path: Path) -> None:
    manifest = tmp_path / "paths"
    manifest.write_bytes(
        b"https:local.md\0--help.md\0bad[a]*?.md\0a file.md\0two\nlines.md\0"
    )
    command = 'source "$1"; link_arguments "$2"; printf "%s\\0" "${link_paths[@]}"'

    result = subprocess.run(
        ("/bin/bash", "-c", command, "bash", str(LINK_CHECK), str(manifest)),
        capture_output=True,
        check=True,
        timeout=5,
    )

    assert result.stdout == (
        b"./https:local.md\0./--help.md\0./bad[[]a[]][*][?].md\0"
        b"./a file.md\0./two\nlines.md\0"
    )


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


def test_ci_bootstrap_keeps_checkout_and_pinned_action_authority() -> None:
    jobs = workflow_jobs()
    links = table(jobs["links"])
    checkout, check = step_list(links)

    assert links["name"] == "Links and anchors"
    assert links["timeout-minutes"] == 10
    assert action_name(checkout["uses"]) == "actions/checkout"
    assert table(checkout["with"])["persist-credentials"] is False
    assert check == {
        "name": "Check repository Markdown offline",
        "run": "bash scripts/ci/check_links.sh",
    }
    for job_id, job in jobs.items():
        steps = step_list(table(job))
        invocations = [
            step
            for step in steps
            if "scripts/ci/setup_scaffold_node.sh" in str(step.get("run", ""))
        ]
        assert len(invocations) == int(job_id == "scaffold-tests")
    source = NODE_SETUP.read_text()
    assert "GITHUB_PATH" not in source
    assert "GITHUB_ENV" not in source
