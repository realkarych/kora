import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.executor.digests import object_digest
from kora.plugins.runtime.local.agent import AgentSources
from kora.protocols.time import SystemClock
from kora.testing.runner import AllowDenyPolicies
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.plugins.runtime.local.test_agent_session import (
    agent_session,
    detached_workload,
)
from tests.plugins.runtime.local.test_command_observation import SUBMITTER, unless_busy
from tests.runner.agent_support import (
    FakeResolver,
    GatedScript,
    execution,
    journal_threads_exited,
    script,
)
from tests.support.local_peers import CONDITION_SECONDS, until

__all__ = ["kora_capture"]


@pytest.mark.asyncio
async def test_logging_context_isolated_and_propagated(
    tmp_path: Path, kora_capture: Capture
) -> None:
    sources = [
        GatedScript(script([turn("m1"), tool_call("Bash", "call-1"), result()]))
        for _ in range(2)
    ]

    async def attempt(index: int) -> None:
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        source = sources[index]
        providers = AgentSources(
            FakeResolver(source, context_for(kora_capture)), AllowDenyPolicies()
        )
        request = execution(deny=("Bash(*)",))
        work = detached_workload(
            {
                "attempt_id": f"attempt-{index + 1}",
                "execution_request": request,
                "execution_request_digest": object_digest(request),
            }
        )
        async with agent_session(root, kora_capture, providers, SystemClock()) as (
            session,
            _,
        ):
            session.accept(SUBMITTER, None, work)
            await unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
            await asyncio.gather(*(item.entered.wait() for item in sources))
            source.release.set()
            await until(lambda: session.status()[0] == "exited")
            assert session.session_observation(SUBMITTER) == session.result_reference()
            await session.stop()

    try:
        await asyncio.wait_for(
            asyncio.gather(*(attempt(index) for index in range(2))), CONDITION_SECONDS
        )
    finally:
        for source in sources:
            source.release.set()
    records = kora_capture.finish()

    for event in (
        "runner.agent.session_started",
        "runner.agent.session_finished",
        "policy.allow_deny.denied",
    ):
        selected = [record for record in records if record.get("event") == event]
        assert sorted(str(record.get("attempt_id")) for record in selected) == [
            "attempt-1",
            "attempt-2",
        ]
    assert all(
        record.get("attempt_id") in {"attempt-1", "attempt-2"}
        for record in records
        if str(record.get("event")).startswith(("runner.agent.", "policy.allow_deny."))
    )
    assert journal_threads_exited()
