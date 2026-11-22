import csv
import io
import json
import shutil
import subprocess
from importlib.metadata import distribution
from pathlib import Path
from typing import cast

import pytest

from kora.db.connection import DatabaseSettings
from kora.plugins.harness.claude.configuration import CLI_VERSION
from kora.plugins.harness.claude.record import BINARY_ENTRY
from tests.cel_eval.children import (
    SITE_PACKAGES,
    ChildResult,
    full_path,
    isolated_path,
    isolated_site,
    record_digest,
    run_child,
)
from tests.cli.test_doctor import configure_installation, statuses, url_text
from tests.observability.test_entry_point_exit_paths import shell
from tests.support.databases import DatabaseKind
from tests.support.provider_tooling import (
    build_fixture_wheel,
    install_measurement_fixture,
)

DOCTOR: str = """
from kora.__main__ import main
sys.exit(main(
    ["doctor", "--json", "--state-root", sys.argv[2]],
    {"KORA_DATABASE_URL": sys.argv[3], "KORA_LOG_LEVEL": "DEBUG"},
))
"""


def configure_harness(root: Path, name: str) -> None:
    (root / "runner.json").write_text(
        json.dumps({"format": "kora.runner/1", "harness": {name: {}}, "policy": {}}),
        encoding="utf-8",
    )


def sdk_entry(name: str) -> bool:
    return name == "claude_agent_sdk" or name.startswith("claude_agent_sdk-")


def altered_sdk(site: Path, marker: Path, *, tampered: bool) -> None:
    installed = distribution("claude-agent-sdk")
    shutil.copytree(
        SITE_PACKAGES / "claude_agent_sdk",
        site / "claude_agent_sdk",
        ignore=shutil.ignore_patterns("_bundled", "__pycache__"),
    )
    metadata_name = f"claude_agent_sdk-{installed.version}.dist-info"
    shutil.copytree(SITE_PACKAGES / metadata_name, site / metadata_name)
    binary = site / BINARY_ENTRY
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    binary.chmod(0o700)
    changed_version = site / "claude_agent_sdk" / "_cli_version.py"
    if not tampered:
        changed_version.write_text('__cli_version__ = "0.0.1"\n', encoding="utf-8")
    record = site / metadata_name / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    for row in rows:
        if row[0] == BINARY_ENTRY and tampered:
            continue
        if row[0] in {BINARY_ENTRY, "claude_agent_sdk/_cli_version.py"}:
            content = (site / row[0]).read_bytes()
            row[1:] = [f"sha256={record_digest(content)}", str(len(content))]
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


@pytest.fixture(scope="module")
def doctor_provider(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("doctor-provider")
    wheel = build_fixture_wheel(
        root,
        {
            "harness.doctor_noisy": (
                "kora_tooling_fixture.doctor_harness:provider_metadata"
            )
        },
    )
    return install_measurement_fixture(root, wheel)


def fixture_doctor(python: Path, root: Path, url: str) -> ChildResult:
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "kora",
            "doctor",
            "--json",
            "--state-root",
            str(root),
        ],
        env={"KORA_DATABASE_URL": url, "KORA_LOG_LEVEL": "DEBUG"},
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    return ChildResult(completed.returncode, completed.stdout, completed.stderr)


@pytest.mark.parametrize(
    ("case", "installation_reason", "harness_name", "harness_reason"),
    [
        ("absent", None, None, "not_configured"),
        ("malformed", "installation_malformed", None, "installation_refused"),
        ("unknown", "installation_provider", None, "installation_refused"),
        ("missing_sdk", "config_error", None, "installation_refused"),
        ("tampered", None, "claude", "integrity_error"),
        ("version", None, "claude", "config_error"),
        ("installed", None, "claude", None),
    ],
)
def test_doctor_reports_harness_installation(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    tmp_path: Path,
    case: str,
    installation_reason: str | None,
    harness_name: str | None,
    harness_reason: str | None,
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir(mode=0o700)
    marker = tmp_path / "binary-started"
    configuration = root / "runner.json"
    path = full_path()
    if case != "absent":
        configure_installation(root)
    if case == "malformed":
        configuration.write_text("{", encoding="utf-8")
    if case == "unknown":
        configure_harness(root, "not_installed")
    if case in {"missing_sdk", "tampered", "version"}:
        site = isolated_site(tmp_path / "site", sdk_entry)
        path = isolated_path(site)
        if case != "missing_sdk":
            altered_sdk(site, marker, tampered=case == "tampered")
    before = sorted(root.iterdir())
    url = url_text(database_settings.url)

    result = run_child(DOCTOR, path, str(root), url)

    assert result.code == (0 if case == "installed" else 1), result.stderr
    report = cast(dict[str, object], json.loads(result.stdout))
    rows = cast(list[dict[str, object]], report["checks"])
    assert [row["check"] for row in rows] == [
        "version",
        "cel",
        "database",
        "state_root",
        "installation",
        "harness",
    ]
    assert rows[2]["detail"] == database_kind.value
    assert rows[4]["reason"] == installation_reason
    assert rows[4]["status"] == ("passed" if installation_reason is None else "failed")
    assert rows[5]["harness"] == harness_name
    assert rows[5]["reason"] == harness_reason
    assert rows[5]["status"] == ("passed" if harness_reason is None else "failed")
    if case == "installed":
        assert rows[5]["detail"] == CLI_VERSION
    if case == "absent":
        assert rows[4]["detail"] == "absent"
    assert not marker.exists()
    assert sorted(root.iterdir()) == before
    assert not list((root / "credentials").glob("*"))
    assert "process.failed" not in result.stderr


def test_doctor_stdout_survives_noisy_providers(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    doctor_provider: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)
    configure_harness(root, "doctor_noisy")
    before = sorted(root.iterdir())

    result = fixture_doctor(doctor_provider, root, url_text(database_settings.url))

    assert result.code == 1, result.stderr
    report = cast(dict[str, object], json.loads(result.stdout))
    assert statuses(report)["installation"] == ("passed", None)
    assert sorted(root.iterdir()) == before
    assert "process.failed" not in result.stderr
    assert statuses(report)["harness:doctor_noisy"] == ("failed", "not_hosted")
    for marker in (
        "doctor-provider-import",
        "doctor-provider-original",
        "doctor-provider-descriptor",
        "doctor-provider-metadata",
    ):
        assert marker in result.stderr
        assert marker not in result.stdout


def test_doctor_closed_stdout_routes_provider_output_to_stderr(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    doctor_provider: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)
    configure_harness(root, "doctor_noisy")
    environment = {
        "KORA_DATABASE_URL": url_text(database_settings.url),
        "KORA_LOG_LEVEL": "DEBUG",
    }

    completed = subprocess.run(
        [
            shell(),
            "-c",
            'exec "$0" -I -m kora doctor --json --state-root "$1" 1>&-',
            str(doctor_provider),
            str(root),
        ],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout == ""
    assert "process.failed" not in completed.stderr
    assert "doctor-provider-descriptor" in completed.stderr
    records = [
        json.loads(line)
        for line in completed.stderr.splitlines()
        if line.startswith("{")
    ]
    assert any(
        record["event"] == "cli.doctor_check_finished"
        and record.get("check") == "harness"
        and record.get("harness") == "doctor_noisy"
        and record.get("status") == "failed"
        and record.get("reason") == "not_hosted"
        for record in records
    )
    assert any(
        record["event"] == "cli.doctor_finished"
        and record.get("exit_code") == 1
        and record.get("failed") == 1
        for record in records
    )
