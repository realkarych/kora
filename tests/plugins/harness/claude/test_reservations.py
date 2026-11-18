import re
from pathlib import Path

import pytest

from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.reservations import LAUNCH_ENV_NAMES, RESERVED_ENV
from tests.support.claude_launch_probe import placements


@pytest.mark.parametrize("telemetry", [False, True])
def test_claude_reserved_env_is_complete_and_well_formed(
    tmp_path: Path, telemetry: bool
) -> None:
    configuration = ClaudeEnvironmentConfiguration(
        binding="claude-test",
        bash_path="/bin/bash",
        max_retries=2,
        cli_telemetry=telemetry,
    )
    required = {
        "ANTHROPIC_*",
        "CLAUDE_*",
        "KORA_*",
        "NODE_*",
        "BUN_*",
        "BASH_FUNC_*",
        "LD_*",
        "DYLD_*",
        "PATH",
        "PWD",
        "TRACEPARENT",
        "TRACESTATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "BASH_ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "ENV",
    }
    proxy_spellings: set[str] = set()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        variants = {""}
        for character in name:
            variants = {
                prefix + letter
                for prefix in variants
                for letter in {character.lower(), character.upper()}
            }
        proxy_spellings.update(variants)

    declared = set(RESERVED_ENV)
    inputs = configuration.launch_inputs(cwd=tmp_path, placements=placements(tmp_path))

    assert len(proxy_spellings) == 1920
    assert proxy_spellings <= declared
    assert required <= declared
    assert len(declared) == len(RESERVED_ENV)
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\*?", entry) for entry in declared)
    assert set(inputs.environment) <= declared
    assert set(inputs.environment) <= set(LAUNCH_ENV_NAMES)
