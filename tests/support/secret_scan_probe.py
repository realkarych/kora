import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from scripts.ci.secret_scan import SCANNER, cached_archive, executable, git, log_options


def commit(repository: Path, content: str) -> str:
    (repository / "value.txt").write_text(content)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "fixture")
    return git(repository, "rev-parse", "HEAD")


def repository(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    return root


def findings(scanner: Path, root: Path, scope: str, output: Path) -> set[str]:
    result = subprocess.run(
        [
            str(scanner),
            "git",
            str(root),
            "--redact=100",
            "--no-banner",
            f"--log-opts={log_options(scope)}",
            "--report-format=json",
            f"--report-path={output}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode not in {0, 1} or not output.is_file():
        raise ValueError("scanner integration failed to produce findings")
    decoded: object = json.loads(output.read_bytes())
    if not isinstance(decoded, list):
        raise ValueError("invalid scanner integration report")
    commits: set[str] = set()
    for item in cast(list[object], decoded):
        if not isinstance(item, dict):
            raise ValueError("invalid scanner finding")
        value: object = cast(dict[str, object], item).get("Commit")
        if not isinstance(value, str):
            raise ValueError("missing finding revision")
        commits.add(value)
    if bool(commits) != (result.returncode == 1):
        raise ValueError("scanner result and findings disagree")
    return commits


def verify_histories(scanner: Path, temporary: Path) -> None:
    token = "gh" + "p_" + "9wA6cR4uF8pN2kV5zM7bD3sQ1tJ0xH6gL4eY"
    root = repository(temporary / "long")
    base = commit(root, "base")
    for index in range(35):
        commit(root, f"main-{index}")
    git(root, "checkout", "-b", "side")
    side = commit(root, f"fixture_token = {token}")
    commit(root, "main-34")
    git(root, "checkout", "master")
    git(root, "merge", "--no-ff", "side", "-m", "merge")
    head = commit(root, f"head_token = {token}")
    observed = findings(scanner, root, f"{base}..{head}", temporary / "long.json")
    if not {side, head} <= observed:
        raise ValueError("scanner omitted side history or actual head")
    root = repository(temporary / "merge")
    base = commit(root, "base")
    git(root, "checkout", "-b", "side")
    commit(root, "side")
    git(root, "checkout", "master")
    commit(root, "main")
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), "merge", "side"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0:
        raise ValueError("merge fixture did not conflict")
    head = commit(root, f"merge_token = {token}")
    if head not in findings(scanner, root, f"{base}..{head}", temporary / "merge.json"):
        raise ValueError("scanner omitted merge-resolution content")
    root = repository(temporary / "clean")
    base = commit(root, "base")
    head = commit(root, "clean")
    if findings(scanner, root, f"{base}..{head}", temporary / "clean.json"):
        raise ValueError("clean fixture produced a finding")


def main() -> None:
    archive = cached_archive(
        Path(os.environ["RUNNER_TOOL_CACHE"]) / "kora-gitleaks-8.30.1", SCANNER
    )
    with TemporaryDirectory(prefix="kora-secret-scan-probe-") as directory:
        temporary = Path(directory)
        scanner = executable(archive, temporary, SCANNER)
        verify_histories(scanner, temporary)
    sys.stdout.write(
        json.dumps(
            {
                "status": "passed",
                "cases": [
                    "history-over30",
                    "deleted-side-content",
                    "actual-head",
                    "merge-resolution",
                    "clean-range",
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
