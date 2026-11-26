import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from kora.conformance.capture import Capture
from kora.plugins.harness.fake.script import Step
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.common import ProtocolContext
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.runtime import SessionObservation, SessionResultObservation
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.runner.agent_support import (
    TEST_DEADLINE_SECONDS,
    HeldWriter,
    SignalledScript,
    SteppedClock,
    advance_batch,
    execution,
    journal_threads_exited,
    make_world,
    run,
    script,
)


class RecordingPolicy(AllowDenyPolicy):
    def __init__(self, writer: HeldWriter) -> None:
        super().__init__()
        self.writer = writer
        self.decisions: list[tuple[str, bool]] = []
        self.decided = asyncio.Event()
        self.writer_states: list[tuple[bool, bool]] = []

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        self.writer_states.append(
            (self.writer.entered.is_set(), self.writer.gate.is_set())
        )
        decision = await super().decide(request, layers, protected_paths, ctx)
        self.decisions.append((request.tool_use_id, decision.allow))
        if len(self.decisions) == 2:
            self.decided.set()
        return decision


class HeldPolicyScript(SignalledScript):
    def __init__(self, writer: HeldWriter, clock: SteppedClock) -> None:
        source = script(
            [
                turn("m1"),
                tool_call("Bash", "allowed-call", arguments={"command": "ls"}),
                tool_call("Bash", "denied-call", arguments={"command": "rm file"}),
                result(),
            ]
        )
        super().__init__(source.header, source.listed)
        self.writer = writer
        self.clock = clock
        self.writer_entered = False

    async def steps(self) -> AsyncGenerator[Step]:
        yield self.listed[0]
        await advance_batch(self.clock)
        self.writer_entered = await asyncio.to_thread(self.writer.entered.wait, 10)
        for item in self.listed[1:]:
            yield item


def test_journal_backpressure_never_blocks_policy_callbacks(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    writer = HeldWriter(journal=world.journal)
    policy = RecordingPolicy(writer)
    source = HeldPolicyScript(writer, clock)
    host = world.host(
        world.resolver(source), policy=policy, journal=writer, clock=clock
    )

    async def played() -> tuple[SessionObservation | None, bool, int]:
        try:
            host.start(
                world.workload(execution_request=execution(deny=("Bash(rm *)",))),
                world.workspace(),
            )
            await asyncio.wait_for(policy.decided.wait(), TEST_DEADLINE_SECONDS)
            gate_open = writer.gate.is_set()
            appended_count = len(writer.appended)
            writer.gate.set()
            await host.finished()
            observation = host.observation()
            return observation, gate_open, appended_count
        finally:
            writer.gate.set()
            await host.stop()
            world.close()

    observation, gate_open, appended_count = run(played())

    assert source.writer_entered
    assert policy.writer_states == [(True, False), (True, False)]
    assert not gate_open
    assert appended_count == 0
    assert isinstance(observation, SessionResultObservation)
    assert policy.decisions == [("allowed-call", True), ("denied-call", False)]
    assert observation.result.stop_reason == "completed"
    assert observation.journal_complete
    assert [
        record["data"]
        for record in world.records()
        if record["kind"] == "permission_denied"
    ] == [
        {
            "tool": "Bash",
            "tool_use_id": "denied-call",
            "source": "policy",
            "pattern": "node.deny.0",
            "code": "denied",
        }
    ]
    assert journal_threads_exited()
