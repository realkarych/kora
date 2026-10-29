import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
REVISION = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Artifact:
    url: str
    archive_digest: str
    binary_digest: str


SCANNER = Artifact(
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz",
    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509",
)


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid event object")
    if any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError("invalid event object")
    return cast(dict[str, object], value)


def command(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f"{name} is required")
    return found


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        [command("git"), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def revision(repository: Path, value: object) -> str:
    if not isinstance(value, str) or REVISION.fullmatch(value) is None:
        raise ValueError("missing or invalid scan revision")
    if git(repository, "rev-parse", "--verify", f"{value}^{{commit}}") != value:
        raise ValueError("unavailable scan revision")
    return value


def scan_range(repository: Path, event_name: str, event: dict[str, object]) -> str:
    if git(repository, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("secret scan requires complete history")
    head = git(repository, "rev-parse", "HEAD")
    if event_name == "pull_request":
        pull_request = object_value(event.get("pull_request"))
        requested = revision(
            repository, object_value(pull_request.get("head")).get("sha")
        )
        base = revision(repository, object_value(pull_request.get("base")).get("sha"))
        if head != requested:
            raise ValueError(
                "secret scan checkout differs from actual pull request head"
            )
        return f"{base}..{requested}"
    if event_name == "push":
        requested = revision(repository, event.get("after"))
        if head != requested:
            raise ValueError("secret scan checkout differs from push head")
        before = event.get("before")
        if before == "0" * 40:
            return requested
        return f"{revision(repository, before)}..{requested}"
    if event_name in {"schedule", "workflow_dispatch"}:
        return "--all"
    raise ValueError("unsupported secret scan event")


def log_options(scope: str) -> str:
    return (
        f"--full-history --diff-merges=first-parent --no-ext-diff --no-textconv {scope}"
    )


def verified_bytes(path: Path, expected: str, limit: int) -> bytes:
    if DIGEST.fullmatch(expected) is None:
        raise ValueError("missing scanner digest pin")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("scanner cache is not a regular file")
        content = stream.read(limit + 1)
    if len(content) > limit or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("scanner digest mismatch")
    return content


def executable(archive: Path, target: Path, artifact: Artifact) -> Path:
    content = verified_bytes(archive, artifact.archive_digest, MAX_ARCHIVE_BYTES)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
        members = [member for member in bundle if member.name == "gitleaks"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("scanner archive lacks a unique regular executable")
        member = members[0]
        if member.size > MAX_BINARY_BYTES:
            raise ValueError("scanner executable exceeds its bound")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError("scanner executable is unavailable")
        with stream:
            binary = stream.read(MAX_BINARY_BYTES + 1)
    if DIGEST.fullmatch(artifact.binary_digest) is None:
        raise ValueError("missing executable digest pin")
    if (
        len(binary) > MAX_BINARY_BYTES
        or hashlib.sha256(binary).hexdigest() != artifact.binary_digest
    ):
        raise ValueError("scanner executable digest mismatch")
    destination = target / "gitleaks"
    with destination.open("xb") as output:
        output.write(binary)
    destination.chmod(0o700)
    verified_bytes(destination, artifact.binary_digest, MAX_BINARY_BYTES)
    return destination


def cached_archive(cache: Path, artifact: Artifact) -> Path:
    if (
        DIGEST.fullmatch(artifact.archive_digest) is None
        or DIGEST.fullmatch(artifact.binary_digest) is None
    ):
        raise ValueError("missing scanner digest pin")
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink() or not stat.S_ISDIR(cache.lstat().st_mode):
        raise ValueError("scanner cache is not an owned directory")
    archive = cache / "gitleaks.tar.gz"
    if not archive.exists() and not archive.is_symlink():
        with TemporaryDirectory(prefix="kora-gitleaks-download-") as directory:
            candidate = Path(directory) / "gitleaks.tar.gz"
            subprocess.run(
                [
                    command("curl"),
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--proto",
                    "=https",
                    "--max-time",
                    "60",
                    artifact.url,
                    "--output",
                    str(candidate),
                ],
                check=True,
                timeout=65,
            )
            content = verified_bytes(
                candidate, artifact.archive_digest, MAX_ARCHIVE_BYTES
            )
            with archive.open("xb") as destination:
                destination.write(content)

    verified_bytes(archive, artifact.archive_digest, MAX_ARCHIVE_BYTES)
    return archive


def main() -> int:
    decoded: object = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = Path.cwd()
    scope = scan_range(
        repository, os.environ["GITHUB_EVENT_NAME"], object_value(decoded)
    )
    archive = cached_archive(
        Path(os.environ["RUNNER_TOOL_CACHE"]) / "kora-gitleaks-8.30.1", SCANNER
    )
    with TemporaryDirectory(prefix="kora-verified-gitleaks-") as directory:
        scanner = executable(archive, Path(directory), SCANNER)
        result = subprocess.run(
            [
                str(scanner),
                "git",
                str(repository),
                "--redact=100",
                "--no-banner",
                f"--log-opts={log_options(scope)}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=480,
        )
    summary = {
        "scanner": "gitleaks-8.30.1",
        "head": git(repository, "rev-parse", "HEAD"),
        "range": scope,
        "exit_code": result.returncode,
    }
    Path("secret-scan.json").write_text(json.dumps(summary, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
