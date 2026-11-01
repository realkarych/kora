import asyncio
import importlib.metadata
import json
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.protocols.harness import LaunchInputs, Placement

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions


def placements(root: Path) -> dict[Placement, Path]:
    return {
        "harness_home": root / "home",
        "harness_tmp": root / "tmp",
        "harness_config": root / "config",
        "harness_input": root / "input",
    }


def configuration(telemetry: bool) -> ClaudeEnvironmentConfiguration:
    return ClaudeEnvironmentConfiguration(
        bash_path="/bin/bash", max_retries=2, cli_telemetry=telemetry
    )


class ResourceAudit:
    def __init__(self) -> None:
        self.count: int = 0

    def observe(self, event: str, _arguments: tuple[object, ...]) -> None:
        if event == "open" or event.startswith(
            ("os.", "pathlib.", "socket.", "subprocess.", "ctypes.")
        ):
            self.count += 1


def pure(root: Path) -> None:
    control = root / "control"
    control.write_bytes(b"audit control")
    threads = threading.enumerate()
    audit = ResourceAudit()
    sys.addaudithook(audit.observe)

    settings = configuration(False)
    inputs = settings.launch_inputs(cwd=root, placements=placements(root))
    events = audit.count
    unchanged_threads = threads == threading.enumerate()
    sdk_imported = any(name.startswith("claude_agent_sdk") for name in sys.modules)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True
    control.read_bytes()

    assert events == 0
    assert unchanged_threads
    assert not running_loop
    assert not sdk_imported
    assert audit.count > events
    assert inputs.environment["HOME"] == str(root / "home")
    assert not (root / "home").exists()
    sys.stdout.write("pure Claude configuration verified\n")


def options(root: Path, telemetry: bool) -> tuple[LaunchInputs, "ClaudeAgentOptions"]:
    from claude_agent_sdk import ClaudeAgentOptions

    from kora.plugins.harness.claude.sdk import launch_options

    paths = placements(root)
    inputs = configuration(telemetry).launch_inputs(cwd=root, placements=paths)
    base = ClaudeAgentOptions(
        model="construction-model",
        env={"OLD_OPTIONS_ENV": "old-options-value"},
        cwd="/old-cwd",
        cli_path="/old-cli",
        setting_sources=[],
        strict_mcp_config=True,
    )

    result = launch_options(base, inputs=inputs, launcher=root / "runtime-launcher")
    paths.clear()
    base.env["LATE_OPTIONS_ENV"] = "late-options-value"

    assert result is not base
    assert result.env == inputs.environment
    assert result.env is not inputs.environment
    assert result.env is not base.env
    assert result.cwd == inputs.cwd == root
    assert result.cli_path == root / "runtime-launcher"
    assert result.model == "construction-model"
    assert result.setting_sources == []
    assert result.strict_mcp_config
    assert base.cwd == "/old-cwd"
    assert base.cli_path == "/old-cli"
    assert base.env["OLD_OPTIONS_ENV"] == "old-options-value"
    return inputs, result


async def handoff(root: Path, telemetry: bool) -> None:
    from kora.conformance.capture import captured
    from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
    from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher
    from kora.observability import Level, LoggingSettings
    from kora.plugins.runtime.local.environment import candidate_environment

    inputs, configured = options(root, telemetry)
    recorder = {"LC_ALL": "C"}
    if "__CF_USER_TEXT_ENCODING" in os.environ:
        recorder["__CF_USER_TEXT_ENCODING"] = os.environ["__CF_USER_TEXT_ENCODING"]
    native = {**configured.env, **recorder}
    expected = {
        "PATH": "/usr/bin:/bin",
        "PWD": str(root),
        "KORA_ATTEMPT_TOKEN": bytes(range(16)).hex(),
        "WORKLOAD_TEXT": "workload-π",
        "TOOL_SECRET": "tool-secret-canary",
        "HARNESS_KEY": "credential-canary",
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "CLAUDE_CONFIG_DIR": str(root / "config"),
        "SHELL": "/bin/bash",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_MAX_RETRIES": "2",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.168",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        **recorder,
    }
    expected[
        "DISABLE_AUTOUPDATER"
        if telemetry
        else "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
    ] = "1"
    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "claude-construction-probe",
        utc_now,
    ) as capture:
        token = capture.scope.admit(bytes(range(16)).hex())
        secret = capture.scope.admit("tool-secret-canary")
        credential = capture.scope.admit("credential-canary")
        registrations = capture.scope.registered_count()
        environment = candidate_environment(
            cwd=inputs.cwd,
            attempt_token=token,
            workload={"WORKLOAD_TEXT": "workload-π"},
            secrets={"TOOL_SECRET": secret},
            harness_credentials={"HARNESS_KEY": credential},
            adapter=native,
        )
        native.clear()
        configured.env.clear()

        outcome = await ProcessLauncher().run(
            LaunchSpec(
                argv=(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("candidate_environment_probe.py")),
                    "record",
                ),
                cwd=root,
                env=environment,
                stdin=json.dumps(expected).encode(),
                timeout_ns=30_000_000_000,
                stdout_limit=1024,
                stderr_limit=4096,
            )
        )

        assert outcome.ending is Ending.EXITED
        assert outcome.exit_code == 0
        assert outcome.stdout == b"candidate environment verified\n"
        assert outcome.stderr == b""
        assert not outcome.stdout_overflow
        assert not outcome.stderr_dropped
        assert not capture.scope.released
        assert capture.scope.registered_count() == registrations
        environment.clear()
    sys.stdout.write("Claude environment handoff verified\n")


def absent(distributions: tuple[str, ...]) -> None:
    for distribution in distributions:
        try:
            importlib.metadata.distribution(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
        raise RuntimeError("unexpected installed dependency")


def main() -> None:
    mode = sys.argv[1]
    root = Path(sys.argv[2])
    if mode == "pure":
        pure(root)
    elif mode == "installed-pure":
        absent(("claude-agent-sdk", "mcp", "pytest", "ruff", "pyright"))
        pure(root)
    elif mode == "installed-options":
        absent(("pytest", "ruff", "pyright"))
        options(root, True)
        options(root, False)
        sys.stdout.write("installed Claude options verified\n")
    elif mode == "handoff":
        asyncio.run(handoff(root, sys.argv[3] == "true"))
    else:
        raise ValueError("unknown Claude probe")


if __name__ == "__main__":
    main()
