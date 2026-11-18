from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.completed import recorded_exited
from kora.protocols.runtime import SessionResultObservation
from kora.runner.identity import NativeIdentityReader
from kora.testing.local import script_item, unless_busy
from tests.plugins.runtime.local.test_completion import completed_entry
from tests.plugins.runtime.local.testing_support import (
    attempt,
    completed_script,
    composition,
    records,
    transfer_outcomes,
)
from tests.support.local_peers import until

__all__ = ["kora_capture"]


@pytest.mark.asyncio
async def test_testing_runner_hosts_fake_on_real_runtime_agent(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        item = script_item(source.serialise())

        handle = await local.run_agent("attempt-1", source)
        result = await unless_busy(
            lambda: local.runtime.session_observation(handle, local.ctx)
        )
        await unless_busy(lambda: local.runtime.quiescence(handle, local.ctx))
        entry = completed_entry(local.runtime)
        await until(lambda: recorded_exited(NativeIdentityReader(), entry.runner))
        status = await local.runtime.status(handle, local.ctx)
        events = records(local)
        transfer = [event for event in events if event.kind == "transfer.materialised"]
        prepared = next(
            event for event in events if event.kind == "runner.workspace_prepared"
        )

        assert ("materialised", "INFO") in transfer_outcomes(local)
        assert isinstance(result, SessionResultObservation)
        assert result.result.stop_reason == "completed"
        assert status.exit_code == 0
        assert events[0].kind == "transfer.materialised"
        assert events[0].data["sha256"] == item.sha256
        assert len(transfer) == 1
        assert transfer[0].data == {
            "name": item.name,
            "size": item.size,
            "sha256": item.sha256,
        }
        assert prepared.data["commit"] == local.commit
        script = attempt(local) / "control/script/script.json"
        assert script.read_bytes() == source.serialise()
        assert not script.is_relative_to(
            Path((await local.workload("attempt-1", source)).workspace.root)
        )


@pytest.mark.asyncio
async def test_testing_runner_hosts_fake_on_real_runtime_command(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        before = {
            str(path.relative_to(local.remote)): path.read_bytes()
            for path in local.remote.rglob("*")
            if path.is_file()
        }

        handle = await local.run_command("attempt-1")
        observation = await unless_busy(
            lambda: local.runtime.command_observation(handle, local.ctx)
        )
        await unless_busy(lambda: local.runtime.quiescence(handle, local.ctx))
        after = {
            str(path.relative_to(local.remote)): path.read_bytes()
            for path in local.remote.rglob("*")
            if path.is_file()
        }

        assert observation.exit_code == 0
        assert before == after
