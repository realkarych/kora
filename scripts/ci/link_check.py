import asyncio
import hashlib
import io
import os
import re
import secrets
import shutil
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ASSET_URL = "https://api.github.com/repos/lycheeverse/lychee/releases/assets/409959156"
ASSET_HOST = "release-assets.githubusercontent.com"
ARCHIVE_SIZE = 7_626_970
BINARY_SIZE = 19_827_680
ARCHIVE_SHA256 = "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a"
BINARY_SHA256 = "87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d"
BINARY_MEMBER = "lychee-x86_64-unknown-linux-gnu/lychee"
MAX_REDIRECT_BYTES = 8192
CURL_VERSION = re.compile(rb"curl ([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3} ")
TOTAL_ROW = re.compile(r"\| 🔍 Total +\| ([1-9][0-9]*) +\|")
INVENTORY_OPTIONS = ("--hidden",)
GLOB_ESCAPES = str.maketrans({"?": "[?]", "*": "[*]", "[": "[[]", "]": "[]]"})


class LinkCheckFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    archive_size: int
    archive_sha256: str
    binary_size: int
    binary_sha256: str


LYCHEE = Asset(ARCHIVE_SIZE, ARCHIVE_SHA256, BINARY_SIZE, BINARY_SHA256)


def program(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise LinkCheckFailed("A required link check program is unavailable.")
    return str(Path(found).resolve())


def private_environment(directory: Path) -> dict[str, str]:
    return {"HOME": str(directory), "TMPDIR": str(directory), "LANG": "C.UTF-8"}


async def stop_child(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        await process.wait()


async def capture(
    executable: str,
    *arguments: str,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL if data is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    communication = asyncio.create_task(process.communicate(data))
    try:
        output, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=35)
    except (TimeoutError, asyncio.CancelledError):
        await stop_child(process)
        await communication
        raise
    if process.returncode != 0:
        raise LinkCheckFailed("A link check prerequisite exited unsuccessfully.")
    return output


def require_curl_version(version: bytes) -> None:
    match = CURL_VERSION.match(version)
    if match is None or (int(match[1]), int(match[2])) < (8, 4):
        raise LinkCheckFailed(
            "A curl version with bounded transfer support is required."
        )


def redirect_config(location: bytes) -> bytes:
    if (
        not 0 < len(location) <= MAX_REDIRECT_BYTES
        or any(byte < 0x21 or byte > 0x7E for byte in location)
        or any(
            character in location for character in (b'"', b"\\", b"[", b"]", b"{", b"}")
        )
        or b"#" in location
    ):
        raise LinkCheckFailed("The Lychee release redirect was refused.")
    url = urlsplit(location.decode("ascii"))
    if (
        url.scheme != "https"
        or url.netloc != ASSET_HOST
        or not url.path.startswith("/")
    ):
        raise LinkCheckFailed("The Lychee release redirect was refused.")
    return b'url = "' + location + b'"\n'


def next_request(response: bytes) -> bytes | None:
    status, _, location = response.partition(b"\n")
    if response == b"200\n\n":
        return None
    if status == b"302" and location.endswith(b"\n"):
        return redirect_config(location[:-1])
    raise LinkCheckFailed("The Lychee release response was refused.")


async def fetch(directory: Path, archive: Path) -> None:
    curl = program("curl")
    env = private_environment(directory)
    try:
        require_curl_version(
            await capture(curl, "--disable", "--version", cwd=directory, env=env)
        )
        arguments = (
            "--disable",
            "--globoff",
            "--silent",
            "--proto",
            "=https",
            "--max-time",
            "30",
            "--max-filesize",
            str(ARCHIVE_SIZE + 1),
            "--output",
            str(archive),
            "--write-out",
            "%{http_code}\n%{redirect_url}\n",
        )
        response = await capture(
            curl,
            *arguments,
            "--header",
            "Accept: application/octet-stream",
            "--url",
            ASSET_URL,
            cwd=directory,
            env=env,
        )
        config = next_request(response)
        if config is not None:
            response = await capture(
                curl, *arguments, "--config", "-", cwd=directory, env=env, data=config
            )
            if response != b"200\n\n":
                raise LinkCheckFailed("The Lychee release response was refused.")
    except (OSError, ValueError, TimeoutError):
        raise LinkCheckFailed(
            "The pinned Lychee archive could not be downloaded."
        ) from None


def verified_bytes(content: bytes, size: int, digest: str) -> bytes:
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise LinkCheckFailed("The pinned Lychee asset failed verification.")
    return content


def executable(archive: Path, destination: Path, asset: Asset) -> Path:
    try:
        with archive.open("rb") as source:
            content = verified_bytes(
                source.read(asset.archive_size + 1),
                asset.archive_size,
                asset.archive_sha256,
            )
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
            members = [member for member in bundle if member.name == BINARY_MEMBER]
            if len(members) != 1 or not members[0].isfile():
                raise LinkCheckFailed(
                    "The Lychee archive lacks a unique regular binary."
                )
            member = members[0]
            if member.size != asset.binary_size:
                raise LinkCheckFailed("The Lychee binary has an unexpected size.")
            stream = bundle.extractfile(member)
            if stream is None:
                raise LinkCheckFailed("The Lychee binary is unavailable.")
            with stream:
                binary = verified_bytes(
                    stream.read(asset.binary_size + 1),
                    asset.binary_size,
                    asset.binary_sha256,
                )
        with destination.open("xb") as target:
            target.write(binary)
        destination.chmod(0o700)
    except (OSError, ValueError, tarfile.TarError):
        raise LinkCheckFailed(
            "The pinned Lychee binary could not be prepared."
        ) from None
    return destination


def markdown_arguments(repository: Path, inventory: bytes) -> tuple[str, ...]:
    paths = inventory.split(b"\0")
    if not inventory or paths.pop() or b"" in paths:
        raise LinkCheckFailed("The Markdown inventory is missing or invalid.")
    filenames = tuple(map(os.fsdecode, paths))
    if any((repository / filename).is_dir() for filename in filenames):
        raise LinkCheckFailed("A selected Markdown input resolves to a directory.")
    return tuple("./" + filename.translate(GLOB_ESCAPES) for filename in filenames)


def report_has_links(report: str) -> bool:
    heading, _, remainder = report.partition("\n\n")
    table, _, _ = remainder.partition("\n\n")
    total = [line for line in table.splitlines() if line.startswith("| 🔍 Total")]
    return (
        heading == "# Summary"
        and len(total) == 1
        and bool(TOTAL_ROW.fullmatch(total[0]))
    )


def write_log(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


async def check_links(
    checker: Path,
    repository: Path,
    private: Path,
    report: Path,
    workflow_url: str,
) -> None:
    arguments = markdown_arguments(
        repository,
        await capture(
            program("git"),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.md",
            cwd=repository,
        ),
    )
    raw_report = private / "report.md"
    marker = secrets.token_hex(32)
    write_log(f"::stop-commands::{marker}\n")
    try:
        process = await asyncio.create_subprocess_exec(
            str(checker),
            "--mode",
            "task",
            "--format",
            "markdown",
            "--output",
            str(raw_report),
            "--config",
            "lychee.toml",
            *INVENTORY_OPTIONS,
            "--",
            *arguments,
            cwd=repository,
            env=private_environment(private),
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            await stop_child(process)
            await process.wait()
            raise
        if not raw_report.is_file():
            raise LinkCheckFailed("Lychee did not produce a Markdown report.")
        content = raw_report.read_text(encoding="utf-8")
        has_links = report_has_links(content)
        report.write_text(
            f"{content}\n[Full Github Actions output]({workflow_url})\n",
            encoding="utf-8",
        )
        write_log(content + "\n")
        if code != 0 or not has_links:
            raise LinkCheckFailed("The offline link and anchor check did not qualify.")
    finally:
        write_log(f"\n::{marker}::\n")


async def run() -> None:
    temporary = Path(os.environ["RUNNER_TEMP"])
    repository = Path.cwd()
    workflow_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    with TemporaryDirectory(prefix="kora-verified-lychee-", dir=temporary) as directory:
        private = Path(directory)
        archive = private / "lychee.tar.gz"
        await fetch(private, archive)
        checker = executable(archive, private / "lychee", LYCHEE)
        await check_links(
            checker, repository, private, temporary / "lychee.md", workflow_url
        )


def main() -> int:
    try:
        asyncio.run(run())
    except LinkCheckFailed as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except (KeyError, OSError, ValueError, TimeoutError):
        sys.stderr.write("The link check could not finish.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
