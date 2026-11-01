import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration
from kora.plugins.runtime.local.runtime import LocalRuntime
from tests.support.local_runtime import (
    ConstructedIdentityReader,
    protocol_error,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]


def test_local_capability_source_has_no_execution_effects(tmp_path: Path) -> None:
    probe = Path(__file__).parents[3] / "support" / "local_capability_probe.py"
    control = tmp_path / "control"
    control.write_bytes(b"audit control")
    missing = tmp_path / "unopened-root"

    result = subprocess.run(
        [sys.executable, "-I", str(probe), str(control), str(missing)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "pure local capability source verified\n"
    assert result.stderr == ""
    assert not missing.exists()


@pytest.mark.asyncio
async def test_opened_local_runtime_uses_configured_capability_source(
    tmp_path: Path, kora_capture: Capture
) -> None:
    configuration = LocalRuntimeConfiguration(state_root=str(state_root(tmp_path)))
    before_open = configuration.capabilities()
    ctx = runtime_context(kora_capture)

    runtime = await LocalRuntime.open(
        configuration, ctx, identities=ConstructedIdentityReader()
    )
    try:
        while_open = runtime.capabilities()
    finally:
        await runtime.close()
    after_close = runtime.capabilities()

    assert before_open == while_open == after_close == configuration.capabilities()


@pytest.mark.asyncio
async def test_unopenable_local_configuration_preserves_pure_inspection(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "unopenable-root"
    root.write_bytes(b"unchanged root")
    root.chmod(0o700)
    configuration = LocalRuntimeConfiguration(state_root=str(root))
    capabilities = configuration.capabilities()
    ctx = runtime_context(kora_capture)

    refusal = await protocol_error(
        LocalRuntime.open(configuration, ctx, identities=ConstructedIdentityReader())
    )

    assert refusal.code == "state_root_unsafe"
    assert not refusal.retryable
    assert refusal.details == {}
    assert root.read_bytes() == b"unchanged root"
    assert configuration.capabilities() == capabilities
