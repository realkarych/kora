import base64
import csv
import errno
import hashlib
import io
import os
import re
import stat
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path

from claude_agent_sdk._cli_version import __cli_version__

from kora.plugins.harness.claude.sdk import bundled_binary
from kora.protocols.errors import KoraProtocolError

BINARY_ENTRY = "claude_agent_sdk/_bundled/claude"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class RecordedFile:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class TrustedBinary:
    path: Path
    sha256: str
    size: int
    cli_version: str


def integrity_error() -> KoraProtocolError:
    return KoraProtocolError(
        "integrity_error",
        "the Claude installation failed integrity verification",
        False,
        {},
    )


def recorded_file(distribution: Distribution, entry: str) -> RecordedFile:
    record = distribution.read_text("RECORD")
    if record is None:
        raise integrity_error()
    try:
        rows = [
            row
            for row in csv.reader(io.StringIO(record), strict=True)
            if row and row[0] == entry
        ]
    except csv.Error:
        raise integrity_error() from None
    if len(rows) != 1 or len(rows[0]) != 3:
        raise integrity_error()
    _, encoded, size_text = rows[0]
    if re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", encoded) is None:
        raise integrity_error()
    digest = base64.urlsafe_b64decode(encoded[7:] + "=")
    if base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii") != encoded[7:]:
        raise integrity_error()
    if re.fullmatch(r"[0-9]{1,19}", size_text) is None:
        raise integrity_error()
    path = Path(str(distribution.locate_file(entry))).absolute()
    return RecordedFile(path, digest.hex(), int(size_text))


def observed_file(path: Path) -> tuple[str, int]:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise integrity_error() from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise integrity_error()
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def verified_binary(distribution: Distribution, cli_version: str) -> TrustedBinary:
    recorded = recorded_file(distribution, BINARY_ENTRY)
    digest, size = observed_file(recorded.path)
    if digest != recorded.sha256 or size != recorded.size:
        raise KoraProtocolError(
            "integrity_error",
            "the Claude binary does not match its installation record",
            False,
            {"expected_sha256": recorded.sha256, "observed_sha256": digest},
        )
    return TrustedBinary(recorded.path, digest, size, cli_version)


def trusted_binary() -> TrustedBinary:
    binary = bundled_binary()
    installed = list(distributions(name="claude-agent-sdk"))
    if len(installed) != 1:
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )
    trusted = verified_binary(installed[0], __cli_version__)
    if trusted.path.resolve() != binary.resolve():
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )
    return trusted
