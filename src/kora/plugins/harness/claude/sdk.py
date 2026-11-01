from dataclasses import replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, __version__
from claude_agent_sdk._cli_version import __cli_version__

from kora.plugins.harness.claude.configuration import CLI_VERSION, SDK_VERSION
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs


def require_native_versions(sdk_version: str, cli_version: str) -> None:
    if sdk_version != SDK_VERSION or cli_version != CLI_VERSION:
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )


def launch_options(
    base: ClaudeAgentOptions, *, inputs: LaunchInputs, launcher: Path
) -> ClaudeAgentOptions:
    require_native_versions(__version__, __cli_version__)
    return replace(
        base, env=dict(inputs.environment), cwd=inputs.cwd, cli_path=launcher
    )
