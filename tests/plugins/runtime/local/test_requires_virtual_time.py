from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.harness import AgentExecution
from kora.testing.local import script_chunks, script_item, unless_busy
from kora.testing.runner import testing_sources as compose_sources
from kora.testing.script import FakeScript
from tests.plugins.runtime.local.testing_support import (
    attempt,
    completed_script,
    composition,
    records,
    transfer_outcomes,
)
from tests.runner.agent_support import execution
from tests.support.local_runtime import protocol_error

__all__ = ["kora_capture"]


def paused_script() -> FakeScript:
    return FakeScript.model_validate(
        {
            "end": "completed",
            "on_interrupt": "acknowledge",
            "steps": [{"kind": "pause"}, {"kind": "result", "status": "success"}],
        }
    )


async def requires_virtual_time_composition(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = paused_script()

        error = await protocol_error(local.run_agent("attempt-1", source))

        assert error.code == "requires_virtual_time"
        assert not error.retryable
        assert await local.runtime.lookup("attempt-1", local.ctx) is None


async def requires_virtual_time_receipt(tmp_path: Path, kora_capture: Capture) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = paused_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.model_dump_json().encode()

        error = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(buffer), script_chunks(buffer), local.ctx
            )
        )
        start = await protocol_error(
            unless_busy(lambda: local.runtime.start(handle, local.ctx))
        )
        corrected = completed_script().serialise()
        later = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(corrected), script_chunks(corrected), local.ctx
            )
        )

        assert ("rejected", "WARNING") in transfer_outcomes(local)
        assert error.code == start.code == "requires_virtual_time"
        assert not error.retryable
        assert later.code == "transfer_refused"
        assert "transfer.rejected" in [event.kind for event in records(local)]
        assert not (attempt(local) / "control/script/script.json").exists()
        assert not (attempt(local) / "control/script/.incoming").exists()


def test_runner_enforced_classes_come_from_enforcement_requires_virtual_time_policy(
    kora_capture: Capture,
) -> None:
    sources = compose_sources(kora_capture.diagnostics)

    policy = sources.policies.policy(AgentExecution.model_validate(execution()))

    assert isinstance(policy, AllowDenyPolicy)


@pytest.mark.parametrize("point", ["composition", "receipt"])
@pytest.mark.asyncio
async def test_runner_enforced_classes_come_from_enforcement_requires_virtual_time(
    point: str, tmp_path: Path, kora_capture: Capture
) -> None:
    leg = (
        requires_virtual_time_composition
        if point == "composition"
        else requires_virtual_time_receipt
    )

    await leg(tmp_path, kora_capture)
