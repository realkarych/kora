import asyncio
import gc
import json
import traceback
import weakref

import pytest

from kora.plugins.harness.codex.app_server_errors import (
    NativeErrorCode,
    NativeErrorFailure,
    NativeErrorRefused,
)
from kora.plugins.harness.codex.app_server_messages import (
    MessageFailure,
    MessageRefused,
)
from kora.plugins.harness.codex.app_server_output import output_candidate
from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import (
    AppServerTurnSession,
    SessionFailure,
    SessionRefused,
    TurnObservations,
)
from kora.plugins.harness.codex.app_server_turn import TerminalStatus
from kora.plugins.harness.codex.app_server_usage import UsageError, UsageFailure
from tests.plugins.harness.codex.test_app_server_errors import (
    detail,
    error_frame,
)
from tests.plugins.harness.codex.test_app_server_errors import (
    notice as error_notice,
)
from tests.plugins.harness.codex.test_app_server_messages import encoded, message
from tests.plugins.harness.codex.test_app_server_output import (
    SCHEMA,
    item,
    terminal_frame,
    terminal_params,
)
from tests.plugins.harness.codex.test_app_server_turn import (
    PROMPT,
    THREAD,
    TURN,
    completed,
    frame,
    started,
)
from tests.plugins.harness.codex.test_app_server_turn_stop import (
    interrupt_request,
    owned_tasks,
    scheduled_barrier,
)
from tests.plugins.harness.codex.test_app_server_usage import (
    FIRST,
    SECOND,
    usage_frame,
)
from tests.plugins.harness.codex.test_app_server_usage import (
    notice as usage_notice,
)
from tests.plugins.harness.codex.test_app_server_usage import (
    snapshot as usage_snapshot,
)
from tests.plugins.harness.codex.wire_fixture import Wire, future_deadline, wire

PRIVATE = "private-session-content"


class Clock:
    value: float = 0.0

    def now(self) -> float:
        return self.value


async def start_request(connection: Wire) -> dict[str, object]:
    request: dict[str, object] = json.loads(await connection.peer_reader.readline())
    return request


@pytest.mark.parametrize("order", ["early", "late", "early-terminal"])
@pytest.mark.asyncio
async def test_combined_state_is_published_only_after_matching_acknowledgement(
    order: str,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        raw_states: list[TurnObservations] = []
        started_states: list[TurnObservations] = []
        observations = (
            usage_frame(),
            encoded(message(text='{"ok":true}', identity="selected-message")),
            error_frame(will_retry=True),
        )
        terminal = terminal_frame(terminal_params([item()]))
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                output_schema=SCHEMA,
                observe=lambda _notice: raw_states.append(session.snapshot()),
                on_started=lambda _turn: started_states.append(session.snapshot()),
            )
        )
        request = await start_request(connection)
        if order == "late":
            frames = (started(), *observations, terminal)
        elif order == "early-terminal":
            frames = (*observations, terminal, started())
        else:
            frames = (*observations, started(), terminal)

        await connection.send(*frames)
        completion = await operation
        state = session.snapshot()
        candidate = output_candidate(completion, thread_id=THREAD)

        assert request["params"] == {
            "threadId": THREAD,
            "input": [{"type": "text", "text": PROMPT}],
            "outputSchema": SCHEMA,
        }
        assert len(raw_states) == 3
        assert len(started_states) == 1
        if order != "late":
            assert raw_states == [TurnObservations()] * 3
            assert started_states[0].usage == usage_snapshot(FIRST)
            assert started_states[0].message_count == 1
            assert started_states[0].latest_error is not None
        assert state.turn_id == TURN
        assert state.usage == usage_snapshot(FIRST)
        assert not state.usage_gap
        assert state.message_count == 1
        assert state.latest_message == completion.final_message
        assert state.latest_error is not None
        assert state.latest_error.will_retry
        assert state.latest_error.info.code is NativeErrorCode.UNAUTHORIZED
        assert state.terminal_status == "completed"
        assert state.terminal_error is None
        assert state.final_message == completion.final_message
        assert candidate is not None
        assert candidate.value == {"ok": True}
        assert owned_tasks() == set()


@pytest.mark.parametrize("kind", ["usage", "message", "error"])
@pytest.mark.asyncio
async def test_provisional_cross_stream_turn_mismatch_never_publishes_state(
    kind: str,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        seen: list[Notification] = []
        mismatch = {
            "usage": encoded(usage_notice(FIRST, turn_id="wrong-turn")),
            "message": encoded(message(turn="wrong-turn")),
            "error": encoded(error_notice(turn_id="wrong-turn")),
        }[kind]
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=seen.append,
            )
        )
        await start_request(connection)

        await connection.send(mismatch, started())
        with pytest.raises((UsageError, MessageRefused, NativeErrorRefused)) as caught:
            await operation

        assert (
            caught.value.reason
            is {
                "usage": UsageFailure.IDENTITY_MISMATCH,
                "message": MessageFailure.IDENTITY_MISMATCH,
                "error": NativeErrorFailure.IDENTITY_MISMATCH,
            }[kind]
        )
        assert len(seen) == 1
        assert session.snapshot() == TurnObservations()
        assert owned_tasks() == set()


@pytest.mark.parametrize("kind", ["usage", "message", "error"])
@pytest.mark.parametrize("wrong_thread", [False, True])
@pytest.mark.asyncio
async def test_later_identity_refusal_keeps_the_previous_confirmed_snapshot(
    kind: str, wrong_thread: bool
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        states: list[TurnObservations] = []
        field = "thread" if wrong_thread else "turn"
        mismatch = {
            "usage": encoded(
                usage_notice(
                    SECOND,
                    thread_id="wrong" if wrong_thread else THREAD,
                    turn_id=TURN if wrong_thread else "wrong",
                )
            ),
            "message": encoded(message(**{field: "wrong"})),
            "error": encoded(
                error_notice(
                    thread_id="wrong" if wrong_thread else THREAD,
                    turn_id=TURN if wrong_thread else "wrong",
                )
            ),
        }[kind]

        def observe(_notice: Notification) -> None:
            states.append(session.snapshot())
            observed.set()

        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=observe,
            )
        )
        await start_request(connection)
        await connection.send(started(), usage_frame())
        await observed.wait()
        previous = session.snapshot()

        await connection.send(mismatch)
        with pytest.raises((UsageError, MessageRefused, NativeErrorRefused)) as caught:
            await operation

        assert (
            caught.value.reason
            is {
                "usage": UsageFailure.IDENTITY_MISMATCH,
                "message": MessageFailure.IDENTITY_MISMATCH,
                "error": NativeErrorFailure.IDENTITY_MISMATCH,
            }[kind]
        )
        assert previous.usage == usage_snapshot(FIRST)
        assert session.snapshot() is previous
        assert len(states) == 1
        assert previous.terminal_status is None
        assert owned_tasks() == set()


@pytest.mark.parametrize("kind", ["usage", "message", "error"])
@pytest.mark.asyncio
async def test_presend_observations_refuse_without_callbacks_or_turn_bytes(
    kind: str,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        seen: list[Notification] = []
        published: list[int] = []
        notices = {
            "usage": usage_frame(),
            "message": encoded(message()),
            "error": error_frame(),
        }
        setup = connection.start(
            connection.rpc.call(
                "fixture", {}, deadline=future_deadline(), observe=lambda _: None
            )
        )
        await connection.peer_reader.readline()
        payload = notices[kind]
        await connection.send(frame({"id": 1, "result": {}}), payload[:13])
        await setup
        await connection.send(payload[13:])

        with pytest.raises((UsageError, MessageRefused, NativeErrorRefused)) as caught:
            await session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=seen.append,
                on_message=lambda ordinal, _: published.append(ordinal),
            )
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert (
            caught.value.reason
            is {
                "usage": UsageFailure.PREMATURE_SNAPSHOT,
                "message": MessageFailure.PREMATURE_MESSAGE,
                "error": NativeErrorFailure.PREMATURE_ERROR,
            }[kind]
        )
        assert seen == []
        assert published == []
        assert session.snapshot() == TurnObservations()
        assert remaining == b""
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_usage_regression_is_sticky_without_replacing_confirmed_counts() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await start_request(connection)
        notices = (
            SECOND,
            FIRST,
            SECOND,
            {**SECOND, "outputTokens": 31, "totalTokens": 191},
        )

        await connection.send(
            started(), *(encoded(usage_notice(value)) for value in notices), completed()
        )
        await operation
        state = session.snapshot()

        assert state.usage is not None
        assert state.usage.counts.output_tokens == 31
        assert state.usage_gap
        assert state.terminal_status == "completed"
        assert state.latest_message is None
        assert state.latest_error is None


@pytest.mark.parametrize("status", ["failed", "interrupted"])
@pytest.mark.asyncio
async def test_terminal_error_retains_only_closed_details_and_no_output(
    status: TerminalStatus,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=16)
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await start_request(connection)
        error = detail({"httpConnectionFailed": {"httpStatusCode": 429}})

        await connection.send(started(), completed(status=status, error=error))
        completion = await operation
        state = session.snapshot()
        candidate = output_candidate(completion, thread_id=THREAD)

        assert state.terminal_status == status
        assert state.terminal_error is not None
        assert state.terminal_error.code is NativeErrorCode.HTTP_CONNECTION_FAILED
        assert state.terminal_error.http_status_code == 429
        assert state.final_message is None
        assert candidate is None
        assert "private-error-wire-canary" not in repr(state)
        assert "private-error-wire-canary" not in repr(state.terminal_error)


@pytest.mark.parametrize(
    "ending", ["cancel", "eof", "truncated", "deadline", "callback", "observer-refused"]
)
@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.asyncio
async def test_failure_preserves_confirmed_partial_state_and_settles_tasks(
    ending: str, controlled: bool
) -> None:
    clock = Clock()
    clock.value = asyncio.get_running_loop().time()
    deadline = clock.value + 100.0
    async with wire(clock=clock.now) as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        notices: list[str] = []
        stop = asyncio.Event() if controlled else None
        callback_failure = SessionRefused(SessionFailure.ALREADY_USED)

        def observe(notification: Notification) -> bool | None:
            if notification.method == "fixture/refuse":
                if ending == "observer-refused":
                    return False
                raise callback_failure
            notices.append(notification.method)
            if len(notices) == 3:
                observed.set()
            return None

        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=deadline,
                stop=stop,
                observe=observe,
            )
        )
        await start_request(connection)
        await connection.send(
            started(), usage_frame(), encoded(message(text=PRIVATE)), error_frame()
        )
        await observed.wait()
        await scheduled_barrier()
        previous = session.snapshot()

        if ending == "cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            if ending in {"eof", "truncated"}:
                if ending == "truncated":
                    await connection.send(b"{")
                connection.peer_writer.close()
                await connection.peer_writer.wait_closed()
            else:
                if ending == "deadline":
                    clock.value = deadline
                await connection.send(frame({"method": "fixture/refuse"}))
            with pytest.raises((RpcError, SessionRefused)) as caught:
                await operation
            if ending == "callback":
                assert caught.value is callback_failure
            else:
                assert isinstance(caught.value, RpcError)
                assert (
                    caught.value.reason
                    is {
                        "eof": RpcFailure.DISCONNECTED,
                        "truncated": RpcFailure.TRUNCATED,
                        "deadline": RpcFailure.DEADLINE,
                        "observer-refused": RpcFailure.OBSERVER_REFUSED,
                    }[ending]
                )

        assert session.snapshot() == previous
        assert previous.usage == usage_snapshot(FIRST)
        assert previous.turn_id == TURN
        assert previous.message_count == 1
        assert previous.latest_message is not None
        assert previous.latest_message.text == PRIVATE
        assert previous.latest_error is not None
        assert previous.terminal_status is None
        assert previous.final_message is None
        assert owned_tasks() == set()
        if stop is not None:
            assert not stop.is_set()


@pytest.mark.parametrize("terminal_first", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
@pytest.mark.asyncio
async def test_stop_races_preserve_actual_terminal_and_combined_usage(
    terminal_first: bool, status: TerminalStatus
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=16)
        stop = asyncio.Event()
        observed = asyncio.Event()
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
                observe=lambda _notice: observed.set(),
            )
        )
        await start_request(connection)
        await connection.send(started(), usage_frame())
        await observed.wait()
        stop.set()
        await interrupt_request(connection)
        reply = frame({"id": 2, "result": {}})
        terminal = completed(status=status)
        frames = (terminal, reply) if terminal_first else (reply, terminal)

        await connection.send(*frames)
        completion = await operation
        state = session.snapshot()

        assert completion.status == status
        assert state.terminal_status == status
        assert state.usage == usage_snapshot(FIRST)
        assert state.final_message is None
        assert stop.is_set()
        assert owned_tasks() == set()


@pytest.mark.parametrize("kind", ["usage", "message", "error", "terminal-error"])
@pytest.mark.asyncio
async def test_malformed_input_cannot_replace_confirmed_state_or_reach_callback(
    kind: str,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        seen: list[Notification] = []

        def observe(notification: Notification) -> None:
            seen.append(notification)
            observed.set()

        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=observe,
            )
        )
        await start_request(connection)
        await connection.send(started(), usage_frame())
        await observed.wait()
        previous = session.snapshot()
        malformed = {
            "usage": encoded(usage_notice({**SECOND, "outputTokens": True})),
            "message": encoded(message(phase=PRIVATE)),
            "error": encoded(error_notice(will_retry=PRIVATE)),
            "terminal-error": completed(status="failed", error=detail(7)),
        }[kind]

        await connection.send(malformed)
        with pytest.raises((UsageError, MessageRefused, NativeErrorRefused)) as caught:
            await operation

        assert (
            caught.value.reason
            is {
                "usage": UsageFailure.INVALID_SNAPSHOT,
                "message": MessageFailure.INVALID_MESSAGE,
                "error": NativeErrorFailure.INVALID_NOTIFICATION,
                "terminal-error": NativeErrorFailure.INVALID_DETAIL,
            }[kind]
        )
        assert len(seen) == 1
        assert session.snapshot() is previous
        assert previous.terminal_status is None
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_terminal_summary_selects_output_without_a_completed_item() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=16)
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await start_request(connection)

        await connection.send(
            started(), terminal_frame(terminal_params([item("null")]))
        )
        completion = await operation
        state = session.snapshot()
        candidate = output_candidate(completion, thread_id=THREAD)

        assert state.message_count == 0
        assert state.latest_message is None
        assert state.final_message is not None
        assert state.final_message.text == "null"
        assert state.terminal_status == "completed"
        assert candidate is not None
        assert candidate.value is None


@pytest.mark.asyncio
async def test_failed_presend_schema_is_one_shot_and_does_not_close_borrowed_rpc() -> (
    None
):
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=16)
        schema: dict[str, object] = {"invalid": object()}

        with pytest.raises(RpcError) as failed:
            await session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                output_schema=schema,
            )
        with pytest.raises(SessionRefused) as reused:
            await session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        await connection.rpc.notify("fixture/healthy", deadline=future_deadline())
        remaining = await connection.peer_reader.readline()

        assert failed.value.reason is RpcFailure.INVALID_OUTBOUND
        assert reused.value.reason is SessionFailure.ALREADY_USED
        assert session.snapshot() == TurnObservations()
        assert json.loads(remaining) == {"method": "fixture/healthy"}


@pytest.mark.asyncio
async def test_item_text_overflow_keeps_the_previous_message_snapshot() -> None:
    async with wire(frame_budget=2_097_152) as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.set(),
            )
        )
        await start_request(connection)
        await connection.send(started(), encoded(message(identity="one", text=PRIVATE)))
        await observed.wait()
        previous = session.snapshot()

        await connection.send(encoded(message(identity="two", text="x" * 1_048_577)))
        with pytest.raises(MessageRefused) as caught:
            await operation

        assert caught.value.reason is MessageFailure.TEXT_LIMIT
        assert session.snapshot() is previous
        assert previous.message_count == 1
        assert previous.latest_message is not None
        assert previous.latest_message.text == PRIVATE
        assert owned_tasks() == set()


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.asyncio
async def test_one_shot_refusal_does_not_write_or_disrupt_the_first_operation(
    concurrent: bool,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=16)
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await start_request(connection)
        if not concurrent:
            await connection.send(started(), completed())
            await operation

        with pytest.raises(SessionRefused) as caught:
            await session.run(
                connection.rpc, prompt=PRIVATE, deadline=future_deadline()
            )
        if concurrent:
            await connection.send(started(), completed())
            await operation
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is SessionFailure.ALREADY_USED
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert session.snapshot().terminal_status == "completed"
        assert remaining == b""
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_replaced_messages_release_and_count_overflow_preserves_state() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.set(),
            )
        )
        await start_request(connection)
        await connection.send(started(), encoded(message(identity="one", text=PRIVATE)))
        await observed.wait()
        previous = session.snapshot()
        assert previous.latest_message is not None
        reference = weakref.ref(previous.latest_message)
        observed.clear()

        await connection.send(encoded(message(identity="two", text="replacement")))
        await observed.wait()
        del previous
        gc.collect()
        latest = session.snapshot()
        await connection.send(encoded(message(identity="three")))
        with pytest.raises(MessageRefused) as caught:
            await operation

        assert reference() is None
        assert latest.message_count == 2
        assert latest.latest_message is not None
        assert latest.latest_message.text == "replacement"
        assert session.snapshot() is latest
        assert caught.value.reason is MessageFailure.MESSAGE_LIMIT
        assert PRIVATE not in repr(latest)
        assert owned_tasks() == set()
