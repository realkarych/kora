import base64
import csv
import hashlib
import os
from importlib.metadata import PathDistribution, distribution
from pathlib import Path

import pytest
from claude_agent_sdk import _cli_version
from claude_agent_sdk._cli_version import __cli_version__

from kora.plugins.harness.claude.configuration import CLI_VERSION
from kora.plugins.harness.claude.integrity import (
    observed_file,
    recorded_file,
    trusted_binary,
    verified_binary,
)
from kora.plugins.harness.claude.sdk import bundled_binary
from kora.protocols.errors import KoraProtocolError

BINARY_ENTRY = "claude_agent_sdk/_bundled/claude"
CONTENT = b"recorded Claude binary"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
ENCODED = (
    base64.urlsafe_b64encode(hashlib.sha256(CONTENT).digest())
    .rstrip(b"=")
    .decode("ascii")
)


def installation(tmp_path: Path, rows: list[list[str]] | None) -> PathDistribution:
    metadata = tmp_path / "claude_agent_sdk-0.2.168.dist-info"
    metadata.mkdir()
    if rows is not None:
        with (metadata / "RECORD").open("w", newline="", encoding="utf-8") as record:
            csv.writer(record).writerows(rows)
    binary = tmp_path / BINARY_ENTRY
    binary.parent.mkdir(parents=True)
    binary.write_bytes(CONTENT)
    return PathDistribution(metadata)


def test_claude_binary_digest_mismatch_prevents_session_start(tmp_path: Path) -> None:
    installed = installation(
        tmp_path, [[BINARY_ENTRY, f"sha256={ENCODED}", str(len(CONTENT))]]
    )
    changed = CONTENT[:-1] + b"!"
    (tmp_path / BINARY_ENTRY).write_bytes(changed)

    with pytest.raises(KoraProtocolError) as raised:
        verified_binary(installed, CLI_VERSION)

    assert raised.value.code == "integrity_error"
    assert raised.value.retryable is False
    assert raised.value.details == {
        "expected_sha256": DIGEST,
        "observed_sha256": hashlib.sha256(changed).hexdigest(),
    }
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize(
    "case",
    [
        "size_mismatch",
        "missing_record",
        "missing_row",
        "duplicate_row",
        "md5",
        "malformed_digest",
        "padded_digest",
        "noncanonical_digest",
        "short_digest",
        "negative_size",
        "invalid_size",
        "oversized_size",
        "missing_size",
        "extra_column",
        "symlink",
        "directory",
        "fifo",
    ],
)
def test_claude_binary_record_refusals(tmp_path: Path, case: str) -> None:
    row = [BINARY_ENTRY, f"sha256={ENCODED}", str(len(CONTENT))]
    rows: list[list[str]] | None = [row]
    if case == "size_mismatch":
        row[2] = str(len(CONTENT) + 1)
    elif case == "missing_record":
        rows = None
    elif case == "missing_row":
        rows = []
    elif case == "duplicate_row":
        rows = [row, row]
    elif case == "md5":
        row[1] = f"md5={ENCODED}"
    elif case == "malformed_digest":
        row[1] = "sha256=" + "!" * 43
    elif case == "padded_digest":
        row[1] += "="
    elif case == "noncanonical_digest":
        row[1] = "sha256=" + "A" * 42 + "B"
    elif case == "short_digest":
        row[1] = "sha256=AAAA"
    elif case == "negative_size":
        row[2] = "-1"
    elif case == "invalid_size":
        row[2] = "1.0"
    elif case == "oversized_size":
        row[2] = "9" * 5000
    elif case == "missing_size":
        row.pop()
    elif case == "extra_column":
        row.append("extra")
    installed = installation(tmp_path, rows)
    binary = tmp_path / BINARY_ENTRY
    if case in {"symlink", "directory", "fifo"}:
        binary.unlink()
        if case == "symlink":
            target = tmp_path / "matching-binary"
            target.write_bytes(CONTENT)
            binary.symlink_to(target)
        elif case == "directory":
            binary.mkdir()
        else:
            os.mkfifo(binary)

    with pytest.raises(KoraProtocolError) as raised:
        verified_binary(installed, CLI_VERSION)

    assert raised.value.code == "integrity_error"
    assert raised.value.retryable is False
    assert raised.value.details == (
        {"expected_sha256": DIGEST, "observed_sha256": DIGEST}
        if case == "size_mismatch"
        else {}
    )
    assert str(tmp_path) not in str(raised.value)


def test_installed_claude_binary_matches_its_record() -> None:
    installed = distribution("claude-agent-sdk")
    recorded = recorded_file(installed, BINARY_ENTRY)
    binary = bundled_binary()

    trusted = trusted_binary()

    assert trusted.path == binary
    assert trusted.sha256 == recorded.sha256
    assert len(trusted.sha256) == 64
    assert trusted.size == recorded.size
    assert trusted.cli_version == CLI_VERSION


def test_claude_cli_version_location_is_pinned() -> None:
    installed = distribution("claude-agent-sdk")
    recorded = recorded_file(installed, "claude_agent_sdk/_cli_version.py")
    module_path = Path(_cli_version.__file__).resolve()

    digest, size = observed_file(module_path)

    assert module_path == recorded.path.resolve()
    assert digest == recorded.sha256
    assert size == recorded.size
    assert __cli_version__ == CLI_VERSION
