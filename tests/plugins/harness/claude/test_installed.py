from kora.conformance.capture import Capture
from kora.plugins.harness.claude.adapter import ClaudeHarness
from kora.plugins.harness.claude.deployment import CONFIG_SCHEMA
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.support.observability import run_python


def test_installed_claude_harness_loads(kora_capture: Capture) -> None:
    metadata = load_provider(ProtocolName.HARNESS, "claude", kora_capture.diagnostics)

    assert metadata.implementation is ClaudeHarness
    assert metadata.protocol is ProtocolName.HARNESS
    assert metadata.name == "claude"
    assert metadata.version == "1"
    assert metadata.config_schema == CONFIG_SCHEMA

    code = """
import sys
from datetime import UTC, datetime

from kora.observability import CATALOG, configure_process, read_logging_settings
from kora.protocols.loading import LOADING_COMPONENT, load_provider
from kora.protocols.metadata import ProtocolName

diagnostics = configure_process(
    LOADING_COMPONENT,
    read_logging_settings({}, CATALOG.components()),
    "claude-loading-probe",
    lambda: datetime.now(UTC),
)
try:
    load_provider(ProtocolName.HARNESS, "claude", diagnostics)
    sys.stdout.write(str("claude_agent_sdk" in sys.modules))
finally:
    assert diagnostics.shutdown(5.0)
"""

    result = run_python(["-I", "-c", code], {})

    assert result.code == 0, result.stderr
    assert result.stdout == b"False"
