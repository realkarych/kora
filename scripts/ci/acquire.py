from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

ATTEMPTS = 3
RETRY_SPACING_SECONDS = 5.0
CONNECT_SECONDS = 15.0
STOP_GRACE_SECONDS = 5.0
FETCH_RESERVE_SECONDS = STOP_GRACE_SECONDS
COMMAND_RESERVE_SECONDS = 2 * STOP_GRACE_SECONDS
JOB_BUDGET_SECONDS = 240.0
BUDGET_LEDGER = "kora-acquisition-budget"
MAX_COMMAND_SECONDS = 120
MAX_LOCATION_BYTES = 8192
READ_BYTES = 1 << 20
SUCCESS_STATUS = "200"
REDIRECT_STATUSES = frozenset({"301", "302", "303", "307", "308"})
UNSAFE_URL_CHARACTERS = frozenset('"\\[]{}#')
SIZE_LIMIT_EXCEEDED = 63
CURL_FAILURES = {
    6: "host name resolution failed",
    7: "connection failed",
    28: "time limit reached",
    35: "TLS handshake failed",
    52: "empty reply",
    56: "connection reset",
    60: "untrusted certificate",
}
USAGE = (
    "usage: acquire.py fetch STORE NAME\n"
    f"       acquire.py run SECONDS(1-{MAX_COMMAND_SECONDS}) -- COMMAND [ARGUMENT...]\n"
)
ASSET_HOST = "release-assets.githubusercontent.com"
NODE_RELEASE = "https://nodejs.org/dist/v24.22.0/"
UV_RELEASE = "https://github.com/astral-sh/uv/releases/download/0.13.1/"
PYTHON_BUILD = "20261013"
PYTHON_RELEASE = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_BUILD}/"
)
LYCHEE_ASSET = (
    "https://api.github.com/repos/lycheeverse/lychee/releases/assets/409959156"
)

Pause = Callable[[float], Awaitable[None]]


class AcquisitionError(Exception):
    pass


class AttemptFailed(Exception):
    pass


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Artifact:
    url: str
    path: str
    size: int
    sha256: str
    redirect_host: str | None
    attempt_seconds: float


def node_archive(platform: str, size: int, sha256: str) -> Artifact:
    filename = f"node-v24.22.0-{platform}.tar.gz"
    return Artifact(NODE_RELEASE + filename, filename, size, sha256, None, 45.0)


def uv_archive(target: str, size: int, sha256: str) -> Artifact:
    return Artifact(
        f"{UV_RELEASE}uv-{target}.tar.gz",
        f"uv-0.13.1-{target}.tar.gz",
        size,
        sha256,
        ASSET_HOST,
        30.0,
    )


def python_archive(version: str, target: str, size: int, sha256: str) -> Artifact:
    filename = f"cpython-{version}+{PYTHON_BUILD}-{target}-install_only_stripped.tar.gz"
    return Artifact(
        PYTHON_RELEASE + filename.replace("+", "%2B"),
        f"{PYTHON_BUILD}/{filename}",
        size,
        sha256,
        ASSET_HOST,
        45.0,
    )


ARTIFACTS: Mapping[str, Artifact] = {
    "node-24.22.0-linux-x64": node_archive(
        "linux-x64",
        58_256_361,
        "28ea0a438de9a13bd7891fd758eee6c7819ee8af5442dfd8d45bd65e56d4228c",
    ),
    "node-24.22.0-darwin-arm64": node_archive(
        "darwin-arm64",
        53_049_576,
        "6a9724d9b50a5f22b4816cded441b5a8d9ddae546191a5e5b7efa5a5e2ee26d5",
    ),
    "uv-0.13.1-x86_64-unknown-linux-gnu": uv_archive(
        "x86_64-unknown-linux-gnu",
        16_738_706,
        "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220",
    ),
    "uv-0.13.1-aarch64-apple-darwin": uv_archive(
        "aarch64-apple-darwin",
        14_280_170,
        "cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2",
    ),
    "cpython-3.11.17-x86_64-unknown-linux-gnu": python_archive(
        "3.11.17",
        "x86_64-unknown-linux-gnu",
        30_823_613,
        "3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522",
    ),
    "cpython-3.11.17-aarch64-apple-darwin": python_archive(
        "3.11.17",
        "aarch64-apple-darwin",
        27_000_974,
        "5e656e09fc73c7ae10e4df66e6aa253f1f5980509b3be8a242e883d67a5fdf26",
    ),
    "cpython-3.15.0-x86_64-unknown-linux-gnu": python_archive(
        "3.15.0",
        "x86_64-unknown-linux-gnu",
        37_656_157,
        "69740dcd9b3480b3d53760f2d3b75582adb14fa54434628dea32480ddede542a",
    ),
    "lychee-0.24.2-x86_64-unknown-linux-gnu": Artifact(
        LYCHEE_ASSET,
        "lychee-0.24.2-x86_64-unknown-linux-gnu.tar.gz",
        7_626_970,
        "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a",
        ASSET_HOST,
        30.0,
    ),
}


@dataclass(frozen=True)
class Budget:
    ledger: Path
    spent: float
    started: float

    def remaining(self) -> float:
        return JOB_BUDGET_SECONDS - self.spent - (time.monotonic() - self.started)

    def exhausted(self, subject: str) -> AcquisitionError:
        return AcquisitionError(
            f"The job acquisition budget of {JOB_BUDGET_SECONDS:g} s is exhausted; "
            f"{subject} was not acquired."
        )

    def allowance(self, limit: float, reserve: float, subject: str) -> float:
        seconds = min(limit, self.remaining() - reserve)
        if seconds <= 0:
            raise self.exhausted(subject)
        return seconds

    async def pause(self, pause: Pause, reserve: float, subject: str) -> None:
        if self.remaining() - reserve <= RETRY_SPACING_SECONDS:
            raise self.exhausted(subject)
        await pause(RETRY_SPACING_SECONDS)

    def record(self) -> None:
        spent = self.spent + time.monotonic() - self.started
        partial = self.ledger.with_name(f".{self.ledger.name}.{os.getpid()}")
        partial.write_text(f"{spent:.3f}\n")
        os.replace(partial, self.ledger)


def opened_budget(ledger: Path) -> Budget:
    text = ledger.read_text() if ledger.exists() else "0"
    try:
        spent = float(text)
    except ValueError:
        raise AcquisitionError(
            f"The acquisition budget ledger {ledger} is invalid."
        ) from None
    if not math.isfinite(spent) or spent < 0:
        raise AcquisitionError(f"The acquisition budget ledger {ledger} is invalid.")
    return Budget(ledger, spent, time.monotonic())


@dataclass(frozen=True)
class Transport:
    curl: str
    home: Path
    authorities: Path | None

    def environment(self) -> dict[str, str]:
        return {"HOME": str(self.home), "TMPDIR": str(self.home), "LANG": "C.UTF-8"}

    def arguments(self, output: Path, size: int, remaining: float) -> list[str]:
        trusted: list[str] = (
            [] if self.authorities is None else ["--cacert", str(self.authorities)]
        )
        return [
            "--disable",
            "--globoff",
            "--silent",
            "--proto",
            "=https",
            "--connect-timeout",
            f"{min(CONNECT_SECONDS, remaining):.3f}",
            "--max-time",
            f"{remaining:.3f}",
            "--max-filesize",
            str(size),
            "--output",
            str(output),
            "--write-out",
            "%{http_code}\n%{redirect_url}\n",
            *trusted,
            "--config",
            "-",
        ]

    async def request(
        self, url: str, output: Path, size: int, deadline: float, accept: bool
    ) -> tuple[str, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AttemptFailed(CURL_FAILURES[28])
        config = f'url = "{url}"\n'
        if accept:
            config += 'header = "Accept: application/octet-stream"\n'
        process = await asyncio.create_subprocess_exec(
            self.curl,
            *self.arguments(output, size, remaining),
            cwd=self.home,
            env=self.environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        exchange = asyncio.create_task(process.communicate(config.encode("ascii")))
        done, _ = await asyncio.wait({exchange}, timeout=remaining + STOP_GRACE_SECONDS)
        if not done:
            stop_process(process)
            await exchange
            raise AttemptFailed(CURL_FAILURES[28])
        written, _ = exchange.result()
        if process.returncode == SIZE_LIMIT_EXCEEDED:
            raise Refused("its size exceeds the pin")
        if process.returncode != 0:
            raise AttemptFailed(curl_failure(process.returncode))
        status, _, location = written.decode("ascii", "replace").partition("\n")
        return status, location.removesuffix("\n")


def stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        return


def curl_failure(code: int | None) -> str:
    meaning = CURL_FAILURES.get(code or 0)
    return f"curl exit status {code}" + ("" if meaning is None else f": {meaning}")


def safe_url(value: str) -> bool:
    return (
        0 < len(value) <= MAX_LOCATION_BYTES
        and all("!" <= character <= "~" for character in value)
        and not UNSAFE_URL_CHARACTERS.intersection(value)
    )


def admitted_redirect(location: str, host: str | None) -> str:
    if host is None:
        raise Refused("the source redirected without an admitted asset host")
    target = urlsplit(location) if safe_url(location) else None
    if (
        target is None
        or target.scheme != "https"
        or target.netloc != host
        or not target.path.startswith("/")
    ):
        raise Refused("the redirect leaves the admitted asset host")
    return location


async def download(
    transport: Transport, artifact: Artifact, candidate: Path, seconds: float
) -> None:
    deadline = time.monotonic() + seconds
    status, location = await transport.request(
        artifact.url, candidate, artifact.size, deadline, True
    )
    if status in REDIRECT_STATUSES and location:
        target = admitted_redirect(location, artifact.redirect_host)
        status, location = await transport.request(
            target, candidate, artifact.size, deadline, False
        )
        if status in REDIRECT_STATUSES:
            raise Refused("the asset host redirected again")
    if status != SUCCESS_STATUS or location:
        raise AttemptFailed(f"HTTP status {status}")


def verified(path: Path, artifact: Artifact) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return False
        while chunk := stream.read(READ_BYTES):
            total += len(chunk)
            if total > artifact.size:
                return False
            digest.update(chunk)
    return total == artifact.size and digest.hexdigest() == artifact.sha256


def real_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError:
        raise AcquisitionError(
            f"The acquisition store path {path} is not a directory."
        ) from None
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise AcquisitionError(f"The acquisition store path {path} is not a directory.")


def located(store: Path, relative: str) -> Path:
    if not store.is_absolute():
        raise AcquisitionError("The acquisition store must be an absolute path.")
    directory = store
    real_directory(directory)
    *parents, name = relative.split("/")
    for part in parents:
        directory = directory / part
        real_directory(directory)
    return directory / name


def report(errors: TextIO, line: str) -> None:
    errors.write(f"{line}\n")
    errors.flush()


async def fetch(
    store: Path,
    name: str,
    artifact: Artifact,
    transport: Transport,
    pause: Pause,
    errors: TextIO,
    budget: Budget,
) -> Path:
    if not safe_url(artifact.url):
        raise AcquisitionError(f"The pinned source of {name} is not a safe URL.")
    destination = located(store, artifact.path)
    if os.path.lexists(destination):
        if not verified(destination, artifact):
            raise AcquisitionError(
                f"Restored {name} failed size or SHA-256 verification; "
                "delete its cache entry."
            )
        return destination
    for attempt in range(1, ATTEMPTS + 1):
        seconds = budget.allowance(
            artifact.attempt_seconds, FETCH_RESERVE_SECONDS, name
        )
        descriptor, partial = tempfile.mkstemp(
            prefix=".partial-", dir=destination.parent
        )
        os.close(descriptor)
        candidate = Path(partial)
        try:
            await download(transport, artifact, candidate, seconds)
            if not verified(candidate, artifact):
                raise Refused("its size or SHA-256 differs from the pin")
            os.replace(candidate, destination)
            return destination
        except Refused as refusal:
            raise AcquisitionError(
                f"Acquisition of {name} refused: {refusal}; not retried."
            ) from None
        except AttemptFailed as failure:
            report(
                errors,
                f"Acquisition attempt {attempt} of {ATTEMPTS} for {name} failed: "
                f"{failure}.",
            )
        finally:
            candidate.unlink(missing_ok=True)
        if attempt < ATTEMPTS:
            await budget.pause(pause, FETCH_RESERVE_SECONDS, name)
    raise AcquisitionError(f"Acquisition of {name} failed after {ATTEMPTS} attempts.")


def signal_group(group: int, number: signal.Signals) -> None:
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        return


async def bounded(command: Sequence[str], seconds: float) -> str | None:
    process = await asyncio.create_subprocess_exec(
        command[0],
        *command[1:],
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    completion = asyncio.create_task(process.wait())
    done, _ = await asyncio.wait({completion}, timeout=seconds)
    if done:
        code = completion.result()
        return None if code == 0 else f"exit status {code}"
    signal_group(process.pid, signal.SIGTERM)
    await asyncio.wait({completion}, timeout=STOP_GRACE_SECONDS)
    signal_group(process.pid, signal.SIGKILL)
    done, _ = await asyncio.wait({completion}, timeout=STOP_GRACE_SECONDS)
    if not done:
        raise AcquisitionError("An acquisition command could not be stopped.")
    return f"time limit of {seconds:g} s reached"


async def run_bounded(
    command: Sequence[str],
    seconds: float,
    pause: Pause,
    errors: TextIO,
    budget: Budget,
) -> None:
    executable = shutil.which(command[0])
    if executable is None:
        raise AcquisitionError(f"The acquisition command {command[0]} is unavailable.")
    line = shlex.join(command)
    for attempt in range(1, ATTEMPTS + 1):
        limit = budget.allowance(seconds, COMMAND_RESERVE_SECONDS, line)
        failure = await bounded((executable, *command[1:]), limit)
        if failure is None:
            return
        report(
            errors,
            f"Acquisition attempt {attempt} of {ATTEMPTS} failed ({failure}): {line}",
        )
        if attempt < ATTEMPTS:
            await budget.pause(pause, COMMAND_RESERVE_SECONDS, line)
    raise AcquisitionError(f"Acquisition failed after {ATTEMPTS} attempts: {line}")


async def fetch_named(
    store: Path,
    name: str,
    artifacts: Mapping[str, Artifact],
    authorities: Path | None,
    pause: Pause,
    errors: TextIO,
    budget: Budget,
) -> Path:
    artifact = artifacts.get(name)
    if artifact is None:
        raise AcquisitionError(f"{name} is not a pinned artifact.")
    curl = shutil.which("curl")
    if curl is None:
        raise AcquisitionError("curl is required to acquire pinned artifacts.")
    with tempfile.TemporaryDirectory(prefix="kora-acquisition-") as home:
        transport = Transport(curl, Path(home), authorities)
        return await fetch(store, name, artifact, transport, pause, errors, budget)


def command_seconds(value: str) -> int | None:
    seconds = int(value) if value.isascii() and value.isdigit() else 0
    return seconds if 1 <= seconds <= MAX_COMMAND_SECONDS else None


async def perform(
    arguments: Sequence[str],
    artifacts: Mapping[str, Artifact],
    authorities: Path | None,
    pause: Pause,
    output: TextIO,
    errors: TextIO,
    budget: Budget,
) -> None:
    if arguments[0] == "fetch":
        path = await fetch_named(
            Path(arguments[1]),
            arguments[2],
            artifacts,
            authorities,
            pause,
            errors,
            budget,
        )
        output.write(f"{path}\n")
        return
    await run_bounded(arguments[3:], int(arguments[1]), pause, errors, budget)


def well_formed(arguments: Sequence[str]) -> bool:
    if len(arguments) == 3 and arguments[0] == "fetch":
        return True
    return (
        len(arguments) > 3
        and arguments[0] == "run"
        and arguments[2] == "--"
        and command_seconds(arguments[1]) is not None
    )


async def main(
    arguments: Sequence[str],
    artifacts: Mapping[str, Artifact],
    authorities: Path | None,
    pause: Pause,
    output: TextIO,
    errors: TextIO,
    ledger: Path | None,
) -> int:
    if not well_formed(arguments):
        errors.write(USAGE)
        return 2
    try:
        if ledger is None:
            raise AcquisitionError(
                "RUNNER_TEMP must name the job's temporary directory, "
                "which holds its acquisition budget."
            )
        budget = opened_budget(ledger)
        try:
            await perform(
                arguments, artifacts, authorities, pause, output, errors, budget
            )
        finally:
            budget.record()
    except AcquisitionError as error:
        report(errors, str(error))
        return 1
    return 0


def job_ledger() -> Path | None:
    temporary = os.environ.get("RUNNER_TEMP")
    return None if not temporary else Path(temporary) / BUDGET_LEDGER


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main(
                sys.argv[1:],
                ARTIFACTS,
                None,
                asyncio.sleep,
                sys.stdout,
                sys.stderr,
                job_ledger(),
            )
        )
    )
