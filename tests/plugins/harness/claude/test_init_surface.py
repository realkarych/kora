import asyncio
import json
from pathlib import Path
from tempfile import mkdtemp

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformanceSink, Trace
from kora.ir import ErrorClass
from kora.plugins.harness.claude.configuration import CLI_VERSION
from kora.plugins.harness.claude.trust import (
    REVIEWED_FILE_EDIT_TOOLS,
    REVIEWED_PLUGINS,
    REVIEWED_TOOLS,
    surface_cause,
)
from kora.plugins.policy.allow_deny.patterns import FILE_EDIT_TOOLS
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.time import SystemClock
from tests.plugins.harness.claude.conformance_launch import RECORDINGS, ClaudeLaunches
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_replay_events import context_for
from tests.plugins.harness.claude.test_session_lifecycle import session_spec


class RecordingPolicy:
    def __init__(self) -> None:
        self.requests: list[ToolCallRequest] = []

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(True, "allowed")


def reviewed_plugins() -> list[dict[str, str]]:
    return [{"name": name, "path": "builtin"} for name in sorted(REVIEWED_PLUGINS)]


def reviewed_init() -> dict[str, object]:
    return {
        "claude_code_version": CLI_VERSION,
        "plugins": reviewed_plugins(),
        "tools": list(REVIEWED_TOOLS),
        "mcp_servers": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "cause"),
    [
        (
            "plugins",
            reviewed_plugins() + [{"name": "unreviewed", "path": "builtin"}],
            "plugin_mismatch",
        ),
        ("tools", [*REVIEWED_TOOLS, "MultiEdit"], "plugin_mismatch"),
        ("tools", [*REVIEWED_TOOLS, "StructuredOutput"], "plugin_mismatch"),
        ("claude_code_version", "2.1.300", "version_mismatch"),
        (
            "mcp_servers",
            [{"name": "account", "status": "connected", "source": "claudeai"}],
            "config_check_failed",
        ),
    ],
)
async def test_init_surface_refuses_before_policy(
    tmp_path: Path,
    claude_capture: Capture,
    field: str,
    value: object,
    cause: str,
) -> None:
    lines = (
        (RECORDINGS / "constructed-callback-deny-2.1.301.ndjson")
        .read_text()
        .splitlines()
    )
    init: dict[str, object] = json.loads(lines[0])
    init[field] = value
    lines[0] = json.dumps(init)
    recording = tmp_path / "changed.ndjson"
    recording.write_text("\n".join(lines) + "\n")
    launches = ClaudeLaunches(Path(mkdtemp(prefix="ki", dir="/tmp")), recording)
    adapter = make_adapter(
        launches.workspace, context_for(claude_capture, SystemClock())
    )
    sink = ConformanceSink(None, Trace())
    policy = RecordingPolicy()

    try:
        async with asyncio.timeout(10):
            result = await adapter.run(session_spec(adapter), policy, sink, launches)

        assert result.stop_reason == cause
        assert result.error_class is ErrorClass.CONFIG_ERROR
        assert policy.requests == []
        assert launches.launch is not None
        assert launches.launch.eof
    finally:
        await launches.close()


@pytest.mark.asyncio
async def test_init_surface_accepts_structured_output(claude_capture: Capture) -> None:
    launches = ClaudeLaunches(
        Path(mkdtemp(prefix="ki", dir="/tmp")),
        RECORDINGS / "conformance-structured_output-2.1.301.ndjson",
    )
    adapter = make_adapter(
        launches.workspace, context_for(claude_capture, SystemClock()), True
    )
    policy = RecordingPolicy()
    sink = ConformanceSink(None, Trace())

    try:
        async with asyncio.timeout(10):
            result = await adapter.run(
                session_spec(adapter, config={"output_schema": {"type": "object"}}),
                policy,
                sink,
                launches,
            )

        assert result.error_class is None
        assert result.stop_reason == "completed"
    finally:
        await launches.close()


def test_claude_reviewed_file_edit_tools_match_policy_family() -> None:
    family = set(FILE_EDIT_TOOLS["claude"])

    assert REVIEWED_FILE_EDIT_TOOLS <= REVIEWED_TOOLS
    assert REVIEWED_FILE_EDIT_TOOLS <= family
    assert REVIEWED_TOOLS & family == REVIEWED_FILE_EDIT_TOOLS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugins", reviewed_plugins() * 2),
        ("tools", [*REVIEWED_TOOLS, "Bash"]),
        ("tools", [*REVIEWED_TOOLS, "StructuredOutput", "StructuredOutput"]),
        ("tools", [*REVIEWED_TOOLS, dict[str, object]()]),
        ("plugins", [None]),
        (
            "plugins",
            [
                {"name": name, "path": "/repository/plugins"}
                for name in sorted(REVIEWED_PLUGINS)
            ],
        ),
        ("mcp_servers", [{"source": list[object]()}]),
    ],
)
def test_surface_cause_refuses_duplicates_and_malformed_shapes(
    field: str, value: object
) -> None:
    init = reviewed_init() | {field: value}

    cause = surface_cause(init, output_format=True)

    assert cause == "plugin_mismatch"


@pytest.mark.parametrize("value", [None, [], {"claude_code_version": []}])
def test_surface_cause_is_total_for_non_init(value: object) -> None:
    assert surface_cause(value, output_format=False) == "plugin_mismatch"


def test_surface_cause_accepts_a_model_dependent_tool_subset() -> None:
    offered = [
        name
        for name in REVIEWED_TOOLS
        if name not in {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}
    ]

    cause = surface_cause(reviewed_init() | {"tools": offered}, output_format=False)

    assert cause is None
