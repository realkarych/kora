import asyncio
from typing import Final, Literal

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.plugins.harness.fake import FakeHarness, ScriptHeader
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    NormalisedEvent,
    RunnerCause,
    SessionResult,
    TurnData,
)
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.runner.calls import PolicyCallFailed
from kora.runner.outcome import CAUSE_CLASSES
from tests.plugins.harness.fake.fake_support import (
    ScriptedSink,
    harness,
    played,
    result,
    scripted,
    tool_call,
    turn,
)
from tests.plugins.harness.fake.test_fake_tool_calls_go_through_policy import (
    AllowDenyClient,
)

End = Literal["completed", "cli_exit_without_result", "oversized_message"]
PAUSE: Final[JsonObject] = {"kind": "pause"}
RUNNER_CAUSES: Final[tuple[RunnerCause, ...]] = (
    "max_events",
    "budget",
    "stall",
    "deadline",
    "cancel",
    "provider_limit",
    "journal_unavailable",
)


class FailingPolicy:
    def __init__(self, kind: Literal["error", "timeout"] | None) -> None:
        self.kind: Literal["error", "timeout"] | None = kind

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        if self.kind is None:
            raise KoraProtocolError(
                "policy_harness_unsupported", "the policy refused", False, {}
            )
        raise PolicyCallFailed(self.kind)


def is_turn(event: NormalisedEvent, message_id: str) -> bool:
    return isinstance(event.data, TurnData) and event.data.message_id == message_id


async def stopped(
    adapter: FakeHarness, sink: ScriptedSink, cause: RunnerCause
) -> SessionResult:
    running = asyncio.create_task(played(adapter, sink))
    sink.stop(cause)
    return await running


def termination_of(capture: Capture) -> object:
    finished = [
        record
        for record in capture.finish()
        if record.get("event") == "harness.fake.session_finished"
    ]
    return finished[-1].get("termination")


@pytest.mark.parametrize(
    ("end", "documents"),
    [
        ("completed", [turn("m1")]),
        ("cli_exit_without_result", [turn("m1"), result()]),
        ("oversized_message", [result(), turn("m1")]),
    ],
)
def test_runner_enforced_classes_come_from_enforcement_end_contradiction(
    end: End, documents: list[JsonObject], kora_capture: Capture
) -> None:
    adapter = harness(scripted(documents, end=end), context_for(kora_capture))

    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(played(adapter, ScriptedSink()))

    assert raised.value.code == "script_end_invalid"


@pytest.mark.parametrize("end", ["max_events", "stall", "deadline", "budget"])
def test_runner_enforced_classes_come_from_enforcement_end_is_closed(
    end: str,
) -> None:
    document: JsonObject = {"end": end, "on_interrupt": "acknowledge"}

    with pytest.raises(ValidationError):
        ScriptHeader.model_validate(document)


@pytest.mark.parametrize("end", ["cli_exit_without_result", "oversized_message"])
def test_runner_enforced_classes_come_from_enforcement_cli_endings(
    end: End, kora_capture: Capture
) -> None:
    adapter = harness(scripted([turn("m1")], end=end), context_for(kora_capture))

    outcome = asyncio.run(played(adapter, ScriptedSink()))

    assert outcome.stop_reason == end
    assert outcome.error_class is ErrorClass.HARNESS_ERROR


@pytest.mark.parametrize("cause", RUNNER_CAUSES)
def test_runner_enforced_classes_come_from_enforcement_interrupt(
    cause: RunnerCause, kora_capture: Capture
) -> None:
    adapter = harness(
        scripted([turn("m1"), PAUSE, result()]), context_for(kora_capture)
    )
    sink = ScriptedSink()

    outcome = asyncio.run(stopped(adapter, sink, cause))

    assert outcome.stop_reason == cause
    assert outcome.error_class is CAUSE_CLASSES[cause]
    assert sink.kinds()[-1] == "result"
    assert termination_of(kora_capture) == "interrupted"


def test_runner_enforced_classes_come_from_enforcement_kill(
    kora_capture: Capture,
) -> None:
    script = scripted([turn("m1"), turn("m2"), PAUSE, result()], on_interrupt="ignore")
    adapter = harness(script, context_for(kora_capture))
    sink = ScriptedSink()

    def observe(event: NormalisedEvent) -> None:
        if is_turn(event, "m1"):
            sink.stop("deadline")
        if is_turn(event, "m2"):
            adapter.kill()

    sink.on_emit = observe

    outcome = asyncio.run(played(adapter, sink))

    assert outcome.stop_reason == "deadline"
    assert outcome.error_class is ErrorClass.TIMEOUT
    assert sink.kinds() == ["turn", "turn"]
    assert termination_of(kora_capture) == "killed_after_grace"


def test_runner_enforced_classes_come_from_enforcement_enforcement(
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), turn("m2"), result()]), context_for(kora_capture)
    )
    sink = ScriptedSink()
    sink.forced = "budget"

    outcome = asyncio.run(played(adapter, sink))

    assert outcome.stop_reason == "budget"
    assert outcome.error_class is ErrorClass.BUDGET_EXHAUSTED
    assert sink.kinds() == ["turn", "result"]


def test_runner_enforced_classes_come_from_enforcement_one_result(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    adapter = harness(
        scripted([turn("m1"), tool_call("Bash", "call-1"), result()]), ctx
    )
    sink = ScriptedSink(max_events=1)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    assert sink.kinds() == ["turn", "result"]
    assert outcome.error_class is ErrorClass.EVENTS_EXHAUSTED


def undecided(is_error: bool, tool_use_id: str = "call-9") -> JsonObject:
    return {
        "kind": "undecided_tool_result",
        "tool": "Bash",
        "tool_use_id": tool_use_id,
        "is_error": is_error,
    }


def test_runner_enforced_classes_come_from_enforcement_bypass(
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), undecided(False), turn("m2"), result()]),
        context_for(kora_capture),
    )
    sink = ScriptedSink()

    outcome = asyncio.run(played(adapter, sink))

    assert outcome.stop_reason == "policy_bypass"
    assert outcome.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.policy_bypass
    assert sink.kinds() == ["turn", "tool_result"]


def test_runner_enforced_classes_come_from_enforcement_held_bypass(
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), undecided(True), result()]), context_for(kora_capture)
    )

    outcome = asyncio.run(played(adapter, ScriptedSink()))

    assert outcome.policy_bypass
    assert outcome.error_class is ErrorClass.HARNESS_ERROR


def test_runner_enforced_classes_come_from_enforcement_held_open(
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), undecided(True)], end="cli_exit_without_result"),
        context_for(kora_capture),
    )

    outcome = asyncio.run(played(adapter, ScriptedSink()))

    assert not outcome.policy_bypass
    assert outcome.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.unconfirmed_calls.total == 1
    assert not outcome.denials_complete


def test_runner_enforced_classes_come_from_enforcement_held_denied(
    kora_capture: Capture,
) -> None:
    denial: JsonObject = {
        "kind": "harness_denial",
        "tool": "Bash",
        "tool_use_id": "call-9",
    }
    adapter = harness(
        scripted([turn("m1"), denial, undecided(True), result()]),
        context_for(kora_capture),
    )

    outcome = asyncio.run(played(adapter, ScriptedSink()))

    assert not outcome.policy_bypass
    assert outcome.error_class is None


@pytest.mark.parametrize(
    ("kind", "cause", "error_class"),
    [
        ("error", "policy_error", ErrorClass.CONFIG_ERROR),
        ("timeout", "policy_timeout", ErrorClass.INFRASTRUCTURE),
        (None, "policy_error", ErrorClass.CONFIG_ERROR),
    ],
)
def test_runner_enforced_classes_come_from_enforcement_policy(
    kind: Literal["error", "timeout"] | None,
    cause: RunnerCause,
    error_class: ErrorClass,
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), tool_call("Bash", "call-1"), result()]),
        context_for(kora_capture),
    )
    sink = ScriptedSink()

    outcome = asyncio.run(played(adapter, sink, FailingPolicy(kind)))

    assert outcome.stop_reason == cause
    assert outcome.error_class is error_class
    assert sink.kinds() == ["turn", "tool_call", "permission_denied"]


async def cancelled_mid_pause(adapter: FakeHarness, sink: ScriptedSink) -> int:
    before = asyncio.all_tasks()
    reached = asyncio.Event()

    def observe(event: NormalisedEvent) -> None:
        reached.set()

    sink.on_emit = observe
    running = asyncio.create_task(played(adapter, sink))
    await reached.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    return len(asyncio.all_tasks() - before)


def test_runner_enforced_classes_come_from_enforcement_cancelled_run(
    kora_capture: Capture,
) -> None:
    adapter = harness(
        scripted([turn("m1"), PAUSE, result()]), context_for(kora_capture)
    )
    sink = ScriptedSink()

    leftover = asyncio.run(cancelled_mid_pause(adapter, sink))

    assert leftover == 0
    assert sink.stopped is not None
    assert not sink.stopped.cancelled()


def test_runner_enforced_classes_come_from_enforcement_sink_survives(
    kora_capture: Capture,
) -> None:
    adapter = harness(scripted([turn("m1"), result()]), context_for(kora_capture))
    sink = ScriptedSink()

    async def finished_then_stopped() -> None:
        await played(adapter, sink)
        sink.stop("stall")

    asyncio.run(finished_then_stopped())

    assert sink.stopped is not None
    assert sink.stopped.result() == "stall"
