import hashlib
import io
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.ci.secret_scan import (
    Artifact,
    cached_archive,
    executable,
    git,
    log_options,
    scan_range,
)


def repository(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    return root


def commit(root: Path, content: str) -> str:
    (root / "value.txt").write_text(content)
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def pull_event(base: str, head: str) -> dict[str, object]:
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


def test_scan_covers_long_history_and_deleted_side_branch_content(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path / "repository")
    base = commit(root, "base")
    for index in range(35):
        commit(root, f"main-{index}")
    git(root, "checkout", "-b", "side")
    introduced = commit(root, "side-only-sensitive-fixture")
    commit(root, "main-34")
    git(root, "checkout", "master")
    git(root, "merge", "--no-ff", "side", "-m", "merge")
    head = commit(root, "actual-head-sensitive-fixture")

    scope = scan_range(root, "pull_request", pull_event(base, head))
    patches = git(root, "log", "-p", *shlex.split(log_options(scope)))

    assert scope.endswith(head)
    assert introduced in patches
    assert "side-only-sensitive-fixture" in patches
    assert "actual-head-sensitive-fixture" in patches
    assert int(git(root, "rev-list", "--count", scope)) > 30


def test_scan_includes_merge_resolution_changes(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    base = commit(root, "base")
    git(root, "checkout", "-b", "side")
    commit(root, "side")
    git(root, "checkout", "master")
    commit(root, "main")
    merge = subprocess.run(
        [shutil.which("git") or "/usr/bin/git", "-C", str(root), "merge", "side"],
        check=False,
        capture_output=True,
    )
    assert merge.returncode != 0
    head = commit(root, "merge-resolution-only-sensitive-fixture")

    scope = scan_range(root, "pull_request", pull_event(base, head))
    patches = git(root, "log", "-p", *shlex.split(log_options(scope)))

    assert "merge-resolution-only-sensitive-fixture" in patches


def test_scan_refuses_missing_or_synthetic_head(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    base = commit(root, "base")
    head = commit(root, "head")
    commit(root, "synthetic-merge")

    with pytest.raises(ValueError, match="actual pull request head"):
        scan_range(root, "pull_request", pull_event(base, head))
    with pytest.raises(subprocess.CalledProcessError):
        scan_range(
            root, "pull_request", pull_event("f" * 40, git(root, "rev-parse", "HEAD"))
        )


def test_initial_push_and_full_history_keep_complete_scope(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    head = commit(root, "head")

    scope = scan_range(root, "push", {"before": "0" * 40, "after": head})

    assert scope == head
    assert scan_range(root, "schedule", {}) == "--all"
    assert scan_range(root, "workflow_dispatch", {}) == "--all"


def archive_fixture(directory: Path) -> tuple[Path, Artifact]:
    binary = b"verified-fixture-executable"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        member = tarfile.TarInfo("gitleaks")
        member.size = len(binary)
        bundle.addfile(member, io.BytesIO(binary))
    path = directory / "gitleaks.tar.gz"
    path.write_bytes(buffer.getvalue())
    return path, Artifact(
        "https://example.invalid/scanner",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        hashlib.sha256(binary).hexdigest(),
    )


def test_cached_archive_and_executable_are_verified_before_use(tmp_path: Path) -> None:
    archive, artifact = archive_fixture(tmp_path)
    output = tmp_path / "private"
    output.mkdir(mode=0o700)

    cached = cached_archive(tmp_path, artifact)
    program = executable(cached, output, artifact)

    assert cached == archive
    assert hashlib.sha256(program.read_bytes()).hexdigest() == artifact.binary_digest
    assert program.stat().st_mode & 0o777 == 0o700
    archive.write_bytes(b"changed-cache")
    with pytest.raises(ValueError, match="digest mismatch"):
        cached_archive(tmp_path, artifact)


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_wrong_binary_pin_refuses_execution(
    tmp_path: Path, missing: bool
) -> None:
    archive, original = archive_fixture(tmp_path)
    artifact = Artifact(
        original.url, original.archive_digest, "" if missing else "f" * 64
    )
    output = tmp_path / "private"
    output.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="digest"):
        executable(archive, output, artifact)

    assert not (output / "gitleaks").exists()
