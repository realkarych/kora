import asyncio
import json
import os
import signal
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.cli.conformance import CommandResult
from kora.ir.json import JsonObject
from kora.protocols.lifetime import (
    CleanupOutcome,
    OriginalOutcome,
)
from kora.protocols.metadata import ProtocolName
from tests.support.configured_conformance import (
    installed_conformance as installed_conformance,
)
from tests.support.configured_conformance import vcs_configuration
from tests.support.provider_tooling import (
    InstalledTooling,
)


@pytest.mark.parametrize("valid", [True, False])
def test_installed_public_conformance_owns_result_and_cleanup(
    installed_conformance: InstalledTooling, tmp_path: Path, valid: bool
) -> None:
    configuration = tmp_path / "private-config.json"
    configuration.write_text(
        json.dumps({"value": "private-public-command-value"}) if valid else "{"
    )
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    result = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(configuration),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
        environment={
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / "state"),
            "KORA_LOG_COMPONENT_LEVELS": "conformance.lifetime=DEBUG",
        },
    )

    assert result.code == (0 if valid else 1), (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["outcome"] == ("complete" if valid else "failed")
    assert report["cleanup"]["state"] == "closed"
    assert report["cleanup"]["original_outcome"] == report["outcome"]
    events = [json.loads(line)["event"] for line in result.stderr.splitlines()]
    assert events.count("conformance.lifetime.started") == 1
    assert events.count("conformance.lifetime.closed") == 1
    if valid:
        assert report["completed"] == report["expected"] == 10
    assert b"private-public-command-value" not in result.stdout + result.stderr
    assert str(configuration).encode() not in result.stdout + result.stderr
    assert not list(home.rglob("journal"))


@pytest.mark.parametrize("mode", ["normal", "prepare", "exercise", "teardown"])
def test_conformance_fixture_contract_is_complete(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-command-config-marker"}))
    data.write_text(json.dumps({"mode": mode, "value": "private-exercise-marker"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_command",
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.outcome is (
        OriginalOutcome.COMPLETE if mode == "normal" else OriginalOutcome.FAILED
    )
    assert process.code == (0 if mode == "normal" else 1)
    if mode in {"normal", "teardown"}:
        assert result.completed == result.expected == 10
    for marker in (
        b"private-command-config-marker",
        b"private-exercise-marker",
        b"private-command-teardown-marker",
        str(config).encode(),
        str(data).encode(),
    ):
        assert marker not in process.stdout + process.stderr
    assert not list(home.rglob("journal"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "shutdown", "crash"])
async def test_conformance_fixture_resources_are_released(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    installed = installed_conformance
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-lifecycle-marker"}))
    with TemporaryDirectory(prefix="kora-feedback-", dir="/tmp") as temporary:
        feedback = str(Path(temporary) / "feedback")
        data.write_text(json.dumps({"mode": mode, "feedback": feedback}))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(feedback)
            listener.listen(1)
            listener.setblocking(False)
            process = await asyncio.create_subprocess_exec(
                str(installed.python),
                "-I",
                "-m",
                "kora",
                "conformance",
                "secrets",
                "configured",
                "--config",
                str(config),
                "--fixtures",
                "kora_tooling_fixture.fixtures_command",
                "--fixture-data",
                str(data),
                cwd=installed.outside,
                env={"HOME": str(home), "PATH": os.defpath, "KORA_LOG_LEVEL": "DEBUG"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(30):
                    loop = asyncio.get_running_loop()
                    endpoint, _ = await loop.sock_accept(listener)
                    with endpoint:
                        invocation = await loop.sock_recv(endpoint, 32)
                        assert len(invocation) == 32

                        if mode == "shutdown":
                            await loop.sock_sendall(endpoint, b"a")
                        else:
                            process.send_signal(
                                signal.SIGKILL if mode == "crash" else signal.SIGTERM
                            )
                        if mode == "cancel":
                            assert process.stderr is not None
                            while (
                                b'"event":"conformance.lifetime.cancelled"'
                                not in await process.stderr.readline()
                            ):
                                assert process.returncode is None
                            process.send_signal(signal.SIGINT)
                        output, errors = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()

    if mode == "crash":
        assert process.returncode == -signal.SIGKILL
        recovered = installed.run(
            "conformance",
            "cleanup",
            invocation.decode("ascii"),
            environment={"HOME": str(home)},
        )
        assert recovered.code == 0, (recovered.stdout, recovered.stderr)
        result = CommandResult.model_validate_json(recovered.stdout)
        assert result.reason == "cleanup_complete"
        output += recovered.stdout
        errors += recovered.stderr
    else:
        result = CommandResult.model_validate_json(output)
        assert process.returncode == (130 if mode == "cancel" else 1)
        assert result.outcome is (
            OriginalOutcome.CANCELLED if mode == "cancel" else OriginalOutcome.FAILED
        )
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.cleanup.unresolved_count == 0
    assert not list(home.rglob("journal"))
    assert b"private-lifecycle-marker" not in output + errors
    assert feedback.encode() not in output + errors


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pre_unlink", "post_unlink", "substituted"])
async def test_cleanup_command_preserves_unverified_socket_recovery(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    installed = installed_conformance
    process = await asyncio.create_subprocess_exec(
        str(installed.python),
        "-I",
        str(Path(__file__).parents[1] / "conformance" / "command_retention_probe.py"),
        str(home),
        mode,
        env={"PATH": os.defpath},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            assert process.stdout is not None
            invocation = (await process.stdout.readexactly(32)).decode("ascii")

            process.kill()
            await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
    assert process.returncode == -signal.SIGKILL
    for _ in range(1 if mode == "post_unlink" else 2):
        completed = installed.run(
            "conformance",
            "cleanup",
            invocation,
            environment={"HOME": str(home), "XDG_STATE_HOME": str(home / "state")},
        )
        report = CommandResult.model_validate_json(completed.stdout)
        assert report.cleanup is not None
        assert completed.code == (0 if mode == "post_unlink" else 1)
        if mode == "post_unlink":
            assert report.cleanup.state is CleanupOutcome.CLOSED
            assert not list(home.rglob("journal"))
        else:
            assert report.cleanup.state is CleanupOutcome.UNKNOWN
            retained = next(home.rglob(invocation))
            assert (retained / "journal").is_file()
            assert (retained / "claim").is_file()
            if mode == "substituted":
                assert report.cleanup.original_outcome is OriginalOutcome.FAILED


@pytest.mark.parametrize("selection", ["configured", "fake", "missing"])
def test_targeted_conformance_loads_only_selected_provider(
    installed_conformance: InstalledTooling, tmp_path: Path, selection: str
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": "private-selection-value"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = {
        "HOME": str(home),
        "KORA_LOG_LEVEL": "DEBUG",
        "PYTHONPATH": str(tmp_path),
        "PYTHONHOME": str(tmp_path),
        "PYTEST_PLUGINS": "unrelated.private_plugin",
        "PYTEST_ADDOPTS": "--private-ambient-option",
        "KORA_TOKEN": "private-environment-value",
    }

    process = installed_conformance.run(
        "conformance",
        "secrets",
        selection,
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
        environment=environment,
    )

    report = CommandResult.model_validate_json(process.stdout)
    assert process.code == (0 if selection == "configured" else 1)
    assert report.cleanup is not None
    assert report.cleanup.state is CleanupOutcome.CLOSED
    records = process.records()
    completed = [
        record for record in records if record["event"] == "cli.conformance_finished"
    ]
    assert len(completed) == 1
    assert completed[0]["outcome"] == report.outcome.value
    assert completed[0]["expected"] == report.expected
    for marker in (
        b"private-selection-value",
        b"unrelated.private_plugin",
        b"private-ambient-option",
        b"private-environment-value",
    ):
        assert marker not in process.stdout + process.stderr


@pytest.mark.parametrize(
    "plugin",
    ["fixtures_missing", "fixtures_wrong_bundle", "fixtures_replaced", "not_installed"],
)
def test_conformance_inputs_are_not_disclosed(
    installed_conformance: InstalledTooling, tmp_path: Path, plugin: str
) -> None:
    config = tmp_path / "private-config-marker.json"
    data = tmp_path / "private-data-marker.json"
    config.write_text(json.dumps({"value": "private-rejected-config-marker"}))
    data.write_text(json.dumps({"value": "private-rejected-exercise-marker"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture." + plugin,
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.completed == 0
    for marker in (
        config.name,
        data.name,
        "private-rejected-config-marker",
        "private-rejected-exercise-marker",
    ):
        assert marker.encode() not in process.stdout + process.stderr


def test_installed_provider_schema_matches_metadata(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-schema-marker"}))
    data.write_text(json.dumps({"mode": "schema"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_command",
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert result.completed == 0
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert b"private-schema-marker" not in process.stdout + process.stderr


@pytest.mark.parametrize(
    ("protocol", "plugin", "configuration", "expected"),
    [
        (ProtocolName.SECRETS, "fixtures_configured", {"value": "secret-value"}, 10),
        (ProtocolName.POLICY, "fixtures_policy", {"deny": ["Bash(git *)"]}, 13),
        (ProtocolName.RUNTIME, "fixtures_runtime", {"value": "configured-domain"}, 5),
        (ProtocolName.HARNESS, "fixtures_harness", {"prefix": "configured-"}, 10),
        (ProtocolName.VCS, "fixtures_vcs", None, 13),
        (
            ProtocolName.ARTIFACT_STORAGE,
            "fixtures_storage",
            {"root": "store", "limits": {"version": 1}},
            18,
        ),
    ],
)
def test_installed_provider_supports_local_tooling(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    protocol: ProtocolName,
    plugin: str,
    configuration: JsonObject | None,
    expected: int,
) -> None:
    installed = installed_conformance
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            configuration
            if configuration is not None
            else vcs_configuration(Path("vcs"))
        )
    )

    process = installed.run(
        "conformance",
        protocol.value,
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture." + plugin,
        environment={
            "HOME": str(home),
            "KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG",
        },
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected == expected
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    records = process.records()
    assert len(records) == 1
    assert records[0]["event"] == "protocols.loading.provider_loaded"
    assert records[0]["protocol"] == protocol.value
    assert not list(home.rglob("journal"))


@pytest.mark.parametrize("value", ["first-effective-value", "second-effective-value"])
def test_installed_provider_configuration_is_effective(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    value: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": value}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert value.encode() not in process.stdout + process.stderr
