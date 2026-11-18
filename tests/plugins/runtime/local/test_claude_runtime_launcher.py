import asyncio
import os
import sys
from pathlib import Path
from typing import Final

import pytest
from pydantic import BaseModel, ConfigDict, PositiveInt

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.config import NodeLimits
from kora.plugins.harness.claude.configuration import (
    CLI_VERSION,
    ClaudeEnvironmentConfiguration,
)
from kora.plugins.harness.claude.options import session_options
from kora.plugins.runtime.local.placements import PLACEMENTS
from kora.protocols.harness import Placement
from kora.protocols.runtime import SessionResultObservation
from tests.plugins.runtime.local.test_agent_session import (
    agent_fields,
    agent_runtime,
    sealed,
)
from tests.plugins.runtime.local.test_command import attempt_directory
from tests.plugins.runtime.local.test_command_observation import unless_busy
from tests.support.local_peers import until
from tests.support.local_runners import submitted

__all__ = ["kora_capture"]

PLATFORM_ENVIRONMENT: Final[frozenset[str]] = (
    frozenset({"__CF_USER_TEXT_ENCODING"}) if sys.platform == "darwin" else frozenset()
)
INTERPRETER_ENVIRONMENT: Final[frozenset[str]] = frozenset({"LC_CTYPE"})
LOCALE_ENVIRONMENT: Final[frozenset[str]] = frozenset({"LC_ALL", "LC_CTYPE", "LANG"})


class ReplayObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    environment: dict[str, str]
    pid: PositiveInt
    launcher_pid: PositiveInt


def process_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_claude_session_receives_only_constructed_environment_from_local_runner(
    tmp_path: Path, kora_capture: Capture
) -> None:
    directory = Path(__file__).parents[2] / "harness" / "claude"
    recording = tmp_path / "recording.ndjson"
    recording.write_bytes(
        (
            directory / "recordings" / "conformance-result_text-2.1.301.ndjson"
        ).read_bytes()
    )
    observation = tmp_path / "environment.json"
    target = tmp_path / "replay"
    player = (
        (directory / "conformance_player.py")
        .read_text()
        .split('if __name__ == "__main__":')[0]
    )
    target.write_text(
        f"#!{sys.executable} -I\n"
        + player
        + "\nassert sys.argv[1:] != ['-v']\n"
        + f"Path({str(observation)!r}).write_text(json.dumps(\n"
        + "{'environment': dict(os.environ), 'pid': os.getpid(), "
        + "'launcher_pid': os.getppid()}))\n"
        + f"play(Path({str(recording)!r}), {CLI_VERSION!r})\n"
    )
    target.chmod(0o500)
    runner = (
        sys.executable,
        "-I",
        str(Path(__file__).with_name("claude_runtime_composition.py")),
        str(target),
    )
    fields = agent_fields()
    fields["session_spec"] = {
        "harness": "claude",
        "adapter_version": CLI_VERSION,
        "options": session_options({}, NodeLimits()).model_dump(mode="json"),
        "files": [],
        "prompt": "replay",
        "resume_handle": None,
        "price_table_id": "test-prices",
        "price_table": {},
    }

    async with agent_runtime(tmp_path, kora_capture, runner) as (runtime, ctx, root):
        handle = await runtime.submit(submitted(runtime, "attempt-1", **fields), ctx)
        await unless_busy(lambda: runtime.start(handle, ctx))
        result = await asyncio.wait_for(sealed(runtime, handle, ctx), 30)
        attempt = attempt_directory(root, runtime, "attempt-1")
        token = (attempt / "attempt.token").read_text()
        candidate = attempt / "candidate"
        placements: dict[Placement, Path] = {
            name: attempt / "harness" / name for name in PLACEMENTS
        }
        expected = dict(
            ClaudeEnvironmentConfiguration(
                binding="replay",
                bash_path="/bin/bash",
                max_retries=0,
                cli_telemetry=False,
            )
            .launch_inputs(cwd=candidate, placements=placements)
            .environment
        )
        expected.update(
            PATH="/usr/bin:/bin", PWD=str(candidate), KORA_ATTEMPT_TOKEN=token
        )
        observed = ReplayObservation.model_validate_json(observation.read_text())
        await until(
            lambda: (
                process_absent(observed.pid) and process_absent(observed.launcher_pid)
            )
        )
        environment = observed.environment
        added_environment = PLATFORM_ENVIRONMENT
        if not LOCALE_ENVIRONMENT.intersection(expected):
            added_environment |= INTERPRETER_ENVIRONMENT
        environment = {
            name: value
            for name, value in environment.items()
            if name not in added_environment
        }

    assert isinstance(result, SessionResultObservation)
    assert result.result.stop_reason == "completed"
    assert result.cause is None
    assert observed.pid != observed.launcher_pid
    assert process_absent(observed.pid)
    assert process_absent(observed.launcher_pid)
    assert environment == expected
    assert not (attempt / "harness").exists()
    assert not (attempt / "control" / "launch").exists()
