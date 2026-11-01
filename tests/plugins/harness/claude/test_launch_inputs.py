import os
import subprocess
import sys
from pathlib import Path
from typing import Final, Literal

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.runtime.local.environment import candidate_environment
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import Placement
from kora.protocols.secret_value import SecretScope
from tests.support.claude_launch_probe import configuration, placements
from tests.support.provider_tooling import (
    REPOSITORY,
    create_virtual_environment,
    run_setup,
)

__all__ = ["kora_capture"]

PROBE: Final[Path] = REPOSITORY / "tests" / "support" / "claude_launch_probe.py"
NATIVE_NAMES: Final[tuple[str, ...]] = (
    "HOME",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "SHELL",
    "ENABLE_CLAUDEAI_MCP_SERVERS",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
    "CLAUDE_CODE_MAX_RETRIES",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_AUTOUPDATER",
)


@pytest.mark.parametrize("telemetry", [False, True])
def test_claude_environment_reaches_real_process(
    tmp_path: Path, telemetry: bool
) -> None:
    environment = {
        "SUPERVISOR_CANARY": "supervisor-only-value",
        "TRACEPARENT": "trace-canary",
        "TRACESTATE": "trace-state-canary",
        "PATH": "/supervisor-path",
        "PWD": "/supervisor-cwd",
        "LC_ALL": "C",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROBE),
            "handoff",
            str(tmp_path),
            str(telemetry).lower(),
        ],
        env=environment,
        check=False,
        capture_output=True,
        timeout=40,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"Claude environment handoff verified\n"
    assert result.stderr == b""


@pytest.mark.parametrize("name", NATIVE_NAMES)
@pytest.mark.parametrize("equal", [False, True])
def test_claude_environment_ownership_cannot_be_overridden(
    tmp_path: Path, kora_capture: Capture, name: str, equal: bool
) -> None:
    inputs = configuration(name == "DISABLE_AUTOUPDATER").launch_inputs(
        cwd=tmp_path, placements=placements(tmp_path)
    )
    scope = SecretScope(kora_capture.diagnostics)
    token = scope.admit(bytes(range(16)).hex())
    scope.release()
    workload = {name: inputs.environment[name] if equal else "private-input-canary"}

    with pytest.raises(KoraProtocolError) as rejected:
        candidate_environment(
            cwd=inputs.cwd,
            attempt_token=token,
            workload=workload,
            secrets={},
            harness_credentials={},
            adapter=inputs.environment,
        )

    assert rejected.value.code == "launch_rejected"
    assert rejected.value.message == "the candidate environment has conflicting owners"
    assert not rejected.value.retryable
    assert rejected.value.details == {}
    assert "private-input-canary" not in str(rejected.value)


def test_claude_configuration_has_no_execution_effects(tmp_path: Path) -> None:
    arguments = [sys.executable, "-I", str(PROBE), "pure", str(tmp_path)]

    result = subprocess.run(arguments, check=False, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"pure Claude configuration verified\n"
    assert result.stderr == b""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bash_path", "private-relative-path"),
        ("bash_path", "/private-path\n"),
        ("max_retries", -1),
        ("max_retries", True),
        ("cli_telemetry", "private-boolean-canary"),
        ("private-config-key-canary", "private-value-canary"),
    ],
)
def test_claude_invalid_configuration_refuses_without_input(
    kora_capture: Capture, field: str, value: object
) -> None:
    supplied: dict[str, object] = {
        "bash_path": "/bin/bash",
        "max_retries": 2,
        "cli_telemetry": False,
    }
    supplied[field] = value

    with pytest.raises(ValidationError) as rejected:
        ClaudeEnvironmentConfiguration.model_validate(supplied)

    assert "private-" not in str(rejected.value)
    assert kora_capture.finish() == []


@pytest.mark.parametrize(
    "field", ["cwd", "harness_home", "harness_tmp", "harness_config"]
)
def test_claude_invalid_launch_paths_are_content_free(
    tmp_path: Path,
    field: Literal["cwd", "harness_home", "harness_tmp", "harness_config"],
) -> None:
    settings = configuration(False)
    paths = placements(tmp_path)
    cwd = tmp_path
    if field == "cwd":
        cwd = Path("private-relative-path")
    else:
        paths[field] = Path("private-relative-path")

    with pytest.raises(KoraProtocolError) as rejected:
        settings.launch_inputs(cwd=cwd, placements=paths)

    assert rejected.value.code == "launch_rejected"
    assert rejected.value.message == "the Claude launch paths are invalid"
    assert not rejected.value.retryable
    assert rejected.value.details == {}
    assert "private-relative-path" not in str(rejected.value)


@pytest.mark.parametrize("missing", ["harness_home", "harness_tmp", "harness_config"])
def test_claude_missing_placement_refuses(tmp_path: Path, missing: Placement) -> None:
    settings = configuration(False)
    paths = placements(tmp_path)
    del paths[missing]

    with pytest.raises(KoraProtocolError) as rejected:
        settings.launch_inputs(cwd=tmp_path, placements=paths)

    assert rejected.value.code == "launch_rejected"
    assert rejected.value.details == {}


@pytest.mark.parametrize(
    ("sdk", "cli"),
    [
        ("0.2.169", "2.1.301"),
        ("0.2.168", "2.1.302"),
        ("", "2.1.301"),
        ("0.2.168", ""),
        ("private-version-canary", "private-cli-canary"),
    ],
)
def test_claude_sdk_version_refusal_is_content_free(sdk: str, cli: str) -> None:
    from kora.plugins.harness.claude.sdk import require_native_versions

    with pytest.raises(KoraProtocolError) as rejected:
        require_native_versions(sdk, cli)

    assert rejected.value.code == "config_error"
    assert rejected.value.message == "the installed Claude SDK version is unsupported"
    assert not rejected.value.retryable
    assert rejected.value.details == {}
    assert "private-" not in str(rejected.value)


@pytest.mark.parametrize("extra", [False, True])
def test_installed_claude_extra_preserves_pure_configuration(
    tmp_path: Path, core_wheel: Path, extra: bool
) -> None:
    python = create_virtual_environment(tmp_path / "installed")
    install = [
        "uv",
        "sync",
        "--locked",
        "--offline",
        "--no-default-groups",
        "--no-build",
        "--no-install-project",
        "--project",
        str(REPOSITORY),
        "--python",
        str(python),
    ]
    if extra:
        install += ["--extra", "claude"]
    run_setup(
        install,
        environment={
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(python.parent.parent),
        },
    )
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(core_wheel),
        ]
    )
    mode = "installed-options" if extra else "installed-pure"

    result = subprocess.run(
        [str(python), "-I", str(PROBE), mode, str(tmp_path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    expected = (
        b"installed Claude options verified\n"
        if extra
        else b"pure Claude configuration verified\n"
    )
    assert result.stdout == expected
