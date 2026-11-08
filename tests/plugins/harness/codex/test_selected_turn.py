import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from itertools import count
from typing import Literal

import pytest

from kora.conformance.harness import ConformanceSink, Trace
from kora.ir.json import JsonObject
from kora.observability.redaction import MASK, SecretRegistry
from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_turn import TurnError, TurnFailure
from kora.plugins.harness.codex.selected_startup import (
    SelectedStartupError,
    SelectedStartupFailure,
)
from kora.plugins.harness.codex.selected_turn import (
    SelectedTurnInputs,
    run_selected_turn,
)
from kora.plugins.harness.codex.terminal_result import TerminalResult
from kora.plugins.harness.codex.usage_events import UsageStamp
from kora.protocols.harness import ResultEvent, TurnEvent, UsageEvent
from kora.runner.usage import UsageLedger
from tests.plugins.harness.codex.test_app_server_messages import encoded, message
from tests.plugins.harness.codex.test_app_server_output import (
    SCHEMA,
    item,
    terminal_frame,
    terminal_params,
)
from tests.plugins.harness.codex.test_app_server_session import Clock
from tests.plugins.harness.codex.test_app_server_turn import TURN, completed
from tests.plugins.harness.codex.test_app_server_turn_stop import scheduled_barrier
from tests.plugins.harness.codex.test_app_server_usage import FIRST, notice
from tests.plugins.harness.codex.test_selected_startup import (
    BEARER,
    IDENTITY,
    INITIALIZE,
    SELECTED,
    THREAD_ID,
    Exchange,
    answer,
    frame,
    initialize,
    receive,
    remaining_bytes,
    selected_exchanges,
)
from tests.plugins.harness.codex.test_usage_events import TIME
from tests.plugins.harness.codex.wire_fixture import Wire, future_deadline, wire

PROMPT = "Complete the selected fixture."
CANARY = "kora-composed-masking-canary"
OUTPUT_TEXT = json.dumps({"ok": True, "note": CANARY})
TURN_START = Exchange(
    "turn/start",
    {
        "threadId": THREAD_ID,
        "input": [{"type": "text", "text": PROMPT}],
        "outputSchema": SCHEMA,
    },
    {"turn": {"id": TURN, "status": "inProgress"}},
)
ACK = frame({"id": 7, "result": TURN_START.result})
USAGE_FRAME = encoded(notice(FIRST, thread_id=THREAD_ID, turn_id=TURN))
MESSAGE_FRAME = encoded(message(thread=THREAD_ID, turn=TURN, text=OUTPUT_TEXT))
TERMINAL_FRAME = terminal_frame(
    terminal_params([item(OUTPUT_TEXT)], thread=THREAD_ID, turn=TURN)
)


def first_stamp() -> UsageStamp:
    return UsageStamp("selected-usage-1", 1)


def selected_operation(
    connection: Wire,
    sink: ConformanceSink,
    registry: SecretRegistry,
    *,
    deadline: float,
    next_stamp: Callable[[], UsageStamp] = first_stamp,
    stop: asyncio.Event | None = None,
) -> asyncio.Task[TerminalResult]:
    return connection.start(
        run_selected_turn(
            connection.rpc,
            IDENTITY,
            SELECTED,
            BEARER,
            SelectedTurnInputs(PROMPT, SCHEMA, True, 2, 2),
            deadline=deadline,
            sink=sink,
            registry=registry,
            usage_message_id="selected-usage",
            source_time=lambda: TIME,
            next_usage_stamp=next_stamp,
            stop=stop,
        )
    )


async def finish_selected_startup(connection: Wire) -> None:
    await initialize(connection)
    for index, exchange in enumerate(selected_exchanges(), 2):
        await answer(connection, index, exchange)


async def prepare_turn(connection: Wire) -> None:
    await finish_selected_startup(connection)
    await receive(connection, 7, TURN_START)


@pytest.mark.parametrize("ending", ["completed", "disconnect", "foreign"])
@pytest.mark.asyncio
async def test_selected_turn_emits_only_confirmed_shared_events(
    ending: Literal["completed", "disconnect", "foreign"],
) -> None:
    sink = ConformanceSink(1, Trace())
    registry = SecretRegistry()
    registration = registry.register(CANARY)

    try:
        async with wire() as connection:
            operation = selected_operation(
                connection, sink, registry, deadline=future_deadline()
            )
            await prepare_turn(connection)

            if ending == "completed":
                await connection.send(USAGE_FRAME, MESSAGE_FRAME, TERMINAL_FRAME, ACK)
                result = await operation
            elif ending == "disconnect":
                await connection.send(ACK, USAGE_FRAME, MESSAGE_FRAME)
                connection.peer_writer.write_eof()
                with pytest.raises(RpcError) as disconnected:
                    await operation
                assert disconnected.value.reason is RpcFailure.DISCONNECTED
                result = None
            else:
                await connection.send(
                    USAGE_FRAME,
                    MESSAGE_FRAME,
                    completed(thread=THREAD_ID, turn="foreign-turn"),
                    ACK,
                )
                with pytest.raises(TurnError) as refused:
                    await operation
                assert refused.value.reason is TurnFailure.IDENTITY_MISMATCH
                result = None
    finally:
        registration.release()

    if ending == "foreign":
        assert sink.events == []
        assert sink.count.events == 0
        return
    assert sink.count.events == 1
    assert sink.enforcement().cause == "max_events"
    (message_event,) = [e for e in sink.events if isinstance(e, TurnEvent)]
    (usage_event,) = [e for e in sink.events if isinstance(e, UsageEvent)]
    assert message_event.data.text == OUTPUT_TEXT.replace(CANARY, MASK)
    assert message_event.data.message_id == "codex-message-1"
    assert usage_event.data.observation_id == "selected-usage-1"
    assert usage_event.data.observed_at == TIME
    assert usage_event.data.coverage.completeness == "partial"
    assert usage_event.data.coverage.attribution_gap
    ledger = UsageLedger()
    ledger.observe(usage_event.data)
    assert ledger.latest == usage_event.data
    assert ledger.rows() == ()
    assert ledger.coverage(result_seen=result is not None).completeness == "unavailable"
    results = [e for e in sink.events if isinstance(e, ResultEvent)]
    if ending == "completed":
        assert result is not None
        assert len(sink.events) == 3
        assert results == [sink.events[-1]]
        assert results[0].data == result.data
        assert result.data.subtype == "codex_completed"
        assert CANARY not in result.data.model_dump_json()
        assert THREAD_ID not in result.data.model_dump_json()
        assert result.output is not None
        assert result.output.value == {
            "ok": True,
            "note": CANARY,
        }
    else:
        assert result is None
        assert len(sink.events) == 2
        assert results == []


@pytest.mark.asyncio
async def test_selected_turn_refused_startup_opens_no_turn_and_emits_nothing() -> None:
    sink = ConformanceSink(None, Trace())
    registry = SecretRegistry()
    local = replace(selected_exchanges()[0], result={"status": "ready"})

    async with wire() as connection:
        operation = selected_operation(
            connection, sink, registry, deadline=future_deadline()
        )
        await initialize(connection)

        await answer(connection, 2, local)
        with pytest.raises(SelectedStartupError) as caught:
            await operation
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is SelectedStartupFailure.LOCAL_STATUS
        assert trailing == b""
        assert sink.events == []


async def stopped_wire(
    connection: Wire, operation: asyncio.Task[TerminalResult]
) -> bytes:
    with pytest.raises(TurnError) as refused:
        await operation
    assert refused.value.reason is TurnFailure.STOP_BEFORE_START
    assert not connection.writer.is_closing()
    return await remaining_bytes(connection)


@pytest.mark.asyncio
async def test_selected_turn_stopped_before_initialize_writes_nothing() -> None:
    sink = ConformanceSink(None, Trace())
    stop = asyncio.Event()
    stop.set()

    async with wire() as connection:
        operation = selected_operation(
            connection, sink, SecretRegistry(), deadline=future_deadline(), stop=stop
        )
        trailing = await stopped_wire(connection, operation)

    assert trailing == b""
    assert sink.events == []


@pytest.mark.parametrize(
    "pending_id",
    [
        pytest.param(1, id="initialize"),
        pytest.param(2, id="local-status"),
        pytest.param(6, id="thread-start"),
    ],
)
@pytest.mark.asyncio
async def test_selected_turn_stopped_during_setup_sends_no_followup(
    pending_id: int,
) -> None:
    sink = ConformanceSink(None, Trace())
    stop = asyncio.Event()
    async with wire() as connection:
        operation = selected_operation(
            connection, sink, SecretRegistry(), deadline=future_deadline(), stop=stop
        )
        pending = INITIALIZE
        if pending_id > 1:
            await initialize(connection)
            exchanges = selected_exchanges()
            for index, exchange in enumerate(exchanges[: pending_id - 2], 2):
                await answer(connection, index, exchange)
            pending = exchanges[pending_id - 2]
        await receive(connection, pending_id, pending)

        stop.set()
        await scheduled_barrier()
        waiting = not operation.done()
        await connection.send(frame({"id": pending_id, "result": pending.result}))
        trailing = await stopped_wire(connection, operation)

    assert waiting
    assert trailing == b""
    assert sink.events == []


@pytest.mark.asyncio
async def test_selected_turn_stop_at_request_id_blocks_write() -> None:
    sink = ConformanceSink(None, Trace())
    stop = asyncio.Event()
    request_ids = count(1)

    def next_request_id() -> int:
        request_id = next(request_ids)
        if request_id == 7:
            stop.set()
        return request_id

    async with wire(request_id=next_request_id) as connection:
        operation = selected_operation(
            connection, sink, SecretRegistry(), deadline=future_deadline(), stop=stop
        )
        await finish_selected_startup(connection)
        trailing = await stopped_wire(connection, operation)

    assert stop.is_set()
    assert trailing == b""
    assert sink.events == []


class PublicationClock(Clock):
    next_value: float | None = None

    def now(self) -> float:
        current = self.value
        if self.next_value is not None:
            self.value = self.next_value
            self.next_value = None
        return current


class ExpiringSink(ConformanceSink):
    def __init__(self, clock: PublicationClock, deadline: float) -> None:
        super().__init__(None, Trace())
        self.clock = clock
        self.deadline = deadline

    def emit(self, kind: str, data: JsonObject) -> None:
        super().emit(kind, data)
        if kind == "usage":
            self.clock.next_value = self.deadline


@pytest.mark.parametrize("expiry", ["stamp", "result"])
@pytest.mark.asyncio
async def test_selected_turn_checks_deadline_at_usage_and_result_publication(
    expiry: Literal["stamp", "result"],
) -> None:
    clock = PublicationClock()
    clock.value = asyncio.get_running_loop().time()
    deadline = future_deadline()
    sink = (
        ConformanceSink(None, Trace())
        if expiry == "stamp"
        else ExpiringSink(clock, deadline)
    )
    registry = SecretRegistry()

    def stamp() -> UsageStamp:
        if expiry == "stamp":
            clock.value = deadline
        return first_stamp()

    async with wire(clock=clock.now) as connection:
        operation = selected_operation(
            connection, sink, registry, deadline=deadline, next_stamp=stamp
        )
        await prepare_turn(connection)

        await connection.send(USAGE_FRAME, TERMINAL_FRAME, ACK)
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is RpcFailure.DEADLINE
        assert [e.kind for e in sink.events] == ([] if expiry == "stamp" else ["usage"])


@pytest.mark.asyncio
async def test_selected_turn_passes_borrowed_stop_and_returns_native_interruption() -> (
    None
):
    sink = ConformanceSink(None, Trace())
    registry = SecretRegistry()
    stop = asyncio.Event()
    interruption = Exchange(
        "turn/interrupt", {"threadId": THREAD_ID, "turnId": TURN}, {}
    )

    async with wire() as connection:
        operation = selected_operation(
            connection, sink, registry, deadline=future_deadline(), stop=stop
        )
        await prepare_turn(connection)

        stop.set()
        await connection.send(ACK)
        await answer(connection, 8, interruption)
        await connection.send(
            completed(thread=THREAD_ID, turn=TURN, status="interrupted")
        )
        result = await operation

        (event,) = sink.events
        assert isinstance(event, ResultEvent)
        assert event.data == result.data
        assert result.data.subtype == "codex_interrupted"
        assert not result.data.is_error
        assert result.failure_class is None
        assert result.output is None
        assert sink.count.events == 0
