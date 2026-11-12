import asyncio
import json
import math
import traceback
from typing import cast

import pytest

from kora.ir.structured_json import MAX_JSON_BYTES
from kora.plugins.harness.codex.app_server_messages import (
    CompletedMessage,
    TurnMessages,
)
from kora.plugins.harness.codex.app_server_output import (
    OutputFailure,
    OutputRefused,
    final_message_candidate,
)
from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import TurnObservations
from kora.plugins.harness.codex.app_server_turn import (
    TerminalStatus,
    TurnCompletion,
    TurnError,
    TurnFailure,
    observe_app_server_turn,
    terminal_observation,
)
from kora.plugins.harness.codex.terminal_result import (
    TerminalResult,
    project_terminal_result,
)
from kora.protocols.harness import OutputCandidate
from tests.plugins.harness.codex.test_app_server_turn import STARTED, frame, started
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

THREAD = "selected-thread"
TURN = "started-turn"
MESSAGE = "selected-message"
PRIVATE = "private-output-canary"
SCHEMA: dict[str, object] = {"type": "object", "required": ["ok"]}


def item(text: object = '{"ok":true}') -> dict[str, object]:
    return {"type": "agentMessage", "id": MESSAGE, "text": text}


def terminal_params(
    items: object,
    *,
    view: object = "summary",
    status: object = "completed",
    thread: object = THREAD,
    turn: object = TURN,
) -> dict[str, object]:
    return {
        "threadId": thread,
        "turn": {
            "id": turn,
            "status": status,
            "error": None,
            "items": items,
            "itemsView": view,
        },
    }


def terminal_frame(params: dict[str, object]) -> bytes:
    return frame({"method": "turn/completed", "params": params})


def summary(text: str) -> CompletedMessage:
    return CompletedMessage(THREAD, TURN, MESSAGE, text, None)


def selected_summary(source: TurnCompletion) -> CompletedMessage:
    message = source.final_message
    assert message is not None
    return message


def projected(source: TurnCompletion) -> TerminalResult:
    observations = TurnObservations(
        turn_id=source.turn_id,
        terminal_status=source.status,
        final_message=source.final_message,
    )
    result = project_terminal_result(
        observations, thread_id=THREAD, output_required=True
    )
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("null", None),
        ("true", True),
        ("7", 7),
        ("7.0", 7.0),
        ('"café 🌲"', "café 🌲"),
        ("[1,false,null]", [1, False, None]),
        (' \n{"ok":true,"value":-0.0}\t', {"ok": True, "value": -0.0}),
    ],
)
def test_completed_summary_text_uses_the_existing_strict_candidate_domain(
    text: str, expected: object
) -> None:
    result = terminal_observation(terminal_params([item(text)]), THREAD)

    candidate = final_message_candidate(
        selected_summary(result), thread_id=THREAD, turn_id=TURN
    )

    assert candidate.source == "result_text"
    assert candidate.value == expected
    assert type(candidate.value) is type(expected)


def test_produced_null_is_present_and_candidate_values_are_detached() -> None:
    source = summary('{"values":[-0.0,1,1.0]}')
    null_source = summary("null")

    first = final_message_candidate(source, thread_id=THREAD, turn_id=TURN)
    second = final_message_candidate(source, thread_id=THREAD, turn_id=TURN)
    null_value = final_message_candidate(null_source, thread_id=THREAD, turn_id=TURN)
    first_value = cast("dict[str, object]", first.value)
    second_value = cast("dict[str, object]", second.value)
    numbers = cast("list[float | int]", first_value["values"])
    sign = math.copysign(1.0, numbers[0])
    first_value["values"] = PRIVATE

    assert sign == -1.0
    assert second_value == {"values": [-0.0, 1, 1.0]}
    assert null_value.value is None
    assert source.text == '{"values":[-0.0,1,1.0]}'


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n\t",
        '```json\n{"ok":true}\n```',
        f'{PRIVATE}: {{"ok":true}}',
        "null null",
        '{"ok":true,"ok":false}',
        '{"x":0,"\\u0078":1}',
        "NaN",
        "Infinity",
        "-Infinity",
        "1e400",
        '"\\ud800"',
        "\ud800",
        "\ufeff{}",
        "[" * 17 + "0" + "]" * 17,
        "1" * 4216,
    ],
    ids=[
        "empty",
        "whitespace",
        "fenced",
        "prefixed",
        "multiple-roots",
        "duplicate-literal",
        "duplicate-escaped",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "overflow",
        "escaped-surrogate",
        "raw-surrogate",
        "bom",
        "depth",
        "integer-bits",
    ],
)
def test_invalid_result_text_never_falls_back_or_exposes_content(text: str) -> None:
    source = summary(text)

    with pytest.raises(OutputRefused) as caught:
        final_message_candidate(source, thread_id=THREAD, turn_id=TURN)

    assert caught.value.reason is OutputFailure.INVALID_JSON
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "text",
    ["x" * (MAX_JSON_BYTES + 1), "é" * (MAX_JSON_BYTES // 2 + 1)],
    ids=["characters", "utf8-bytes"],
)
def test_raw_text_admission_precedes_decoding_and_enforces_utf8_bytes(
    text: str,
) -> None:
    source = summary(text)

    with pytest.raises(OutputRefused) as caught:
        final_message_candidate(source, thread_id=THREAD, turn_id=TURN)

    assert caught.value.reason is OutputFailure.TEXT_LIMIT


def test_exact_raw_and_canonical_limits_are_preserved() -> None:
    exact = summary('"' + "a" * (MAX_JSON_BYTES - 2) + '"')
    expansion = summary("[" + ",".join(["1e15"] * 60_000) + "]")

    accepted = final_message_candidate(exact, thread_id=THREAD, turn_id=TURN)
    with pytest.raises(OutputRefused) as caught:
        final_message_candidate(expansion, thread_id=THREAD, turn_id=TURN)

    assert isinstance(accepted.value, str)
    assert len(accepted.value) == MAX_JSON_BYTES - 2
    assert caught.value.reason is OutputFailure.INVALID_JSON


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_non_success_ignores_even_an_invalid_summary(status: TerminalStatus) -> None:
    source = TurnCompletion(
        TURN, status, None, CompletedMessage(PRIVATE, PRIVATE, MESSAGE, "NaN", None)
    )

    result = projected(source)

    assert result.output is None
    assert result.output_diagnostic is None


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_native_non_success_does_not_project_summary_payload(
    status: TerminalStatus,
) -> None:
    params = terminal_params({PRIVATE: PRIVATE}, status=status)

    result = terminal_observation(params, THREAD)
    candidate = projected(result).output

    assert result.status == status
    assert result.final_message is None
    assert candidate is None


def test_legacy_completion_without_summary_has_no_candidate() -> None:
    result = TurnCompletion(TURN, "completed", None)

    candidate = projected(result).output

    assert candidate is None


@pytest.mark.parametrize("view", [None, "notLoaded", "full", "future", []])
def test_non_summary_views_supply_no_inferred_candidate(view: object) -> None:
    source = terminal_observation(terminal_params([item()], view=view), THREAD)

    result = projected(source).output

    assert source.final_message is None
    assert result is None


@pytest.mark.parametrize(
    "items",
    [
        None,
        {},
        [],
        [item(), item()],
        [{"type": "userMessage", "id": MESSAGE}],
        [item(None)],
        [item("\ud800")],
        [{"type": "agentMessage", "id": "", "text": PRIVATE}],
        [{"type": "agentMessage", "id": MESSAGE, "text": PRIVATE, "phase": PRIVATE}],
    ],
)
def test_malformed_recognized_summaries_refuse_safely(items: object) -> None:
    params = terminal_params(items)

    with pytest.raises(TurnError) as caught:
        terminal_observation(params, THREAD)

    assert caught.value.reason is TurnFailure.INVALID_COMPLETION
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("wrong_thread", [False, True])
def test_projection_refuses_mismatched_selected_identity(wrong_thread: bool) -> None:
    message = CompletedMessage(
        PRIVATE if wrong_thread else THREAD,
        TURN if wrong_thread else PRIVATE,
        MESSAGE,
        "null",
        None,
    )

    with pytest.raises(OutputRefused) as caught:
        final_message_candidate(message, thread_id=THREAD, turn_id=TURN)

    assert caught.value.reason is OutputFailure.IDENTITY_MISMATCH
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("schema", [{}, SCHEMA])
@pytest.mark.asyncio
async def test_real_wire_schema_and_summary_project_through_the_core(
    early: bool, controlled: bool, schema: dict[str, object]
) -> None:
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=2, max_text_bytes=1024)

        def observe(notice: Notification) -> None:
            state.accept(notice)

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                output_schema=schema,
                observe=observe,
                stop=asyncio.Event() if controlled else None,
            )
        )
        request = await connection.peer_reader.readline()
        message = frame(
            {
                "method": "item/completed",
                "params": {"threadId": THREAD, "turnId": TURN, "item": item()},
            }
        )
        terminal = terminal_frame(terminal_params([item()]))
        events = (
            (message, terminal, started()) if early else (started(), message, terminal)
        )

        await connection.send(*events)
        result = await operation
        candidate = final_message_candidate(
            selected_summary(result), thread_id=THREAD, turn_id=TURN
        )

        decoded = cast("dict[str, object]", json.loads(request))
        sent_params = cast("dict[str, object]", decoded["params"])
        assert sent_params["outputSchema"] == schema
        assert result.final_message == state.for_turn(TURN)
        assert state.count == 1
        assert candidate.value == {"ok": True}


@pytest.mark.asyncio
async def test_schema_snapshot_survives_mutation_during_pre_send_observation() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        schema: dict[str, object] = {"required": ["ok"]}

        def mutate(notice: Notification) -> None:
            schema["required"] = [PRIVATE]

        setup = connection.start(
            connection.rpc.call("setup", {}, deadline=deadline, observe=lambda _: None)
        )
        await connection.peer_reader.readline()
        await connection.send(
            frame({"id": 1, "result": {}}), frame({"method": "notice"})
        )
        await setup

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=deadline,
                output_schema=schema,
                observe_before_send=mutate,
            )
        )
        request = await connection.peer_reader.readline()
        await connection.send(
            frame({"id": 2, "result": STARTED}),
            terminal_frame(terminal_params([item()])),
        )
        result = await operation

        assert b'"outputSchema":{"required":["ok"]}' in request
        assert PRIVATE.encode() not in request
        assert schema == {"required": [PRIVATE]}
        assert result.status == "completed"


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_terminal_summary_cannot_bypass_acknowledged_turn_identity(
    early: bool,
) -> None:
    async with wire() as connection:
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                output_schema=SCHEMA,
            )
        )
        await connection.peer_reader.readline()
        terminal = terminal_frame(terminal_params([item()], turn=PRIVATE))
        events = (terminal, started()) if early else (started(), terminal)

        await connection.send(*events)
        with pytest.raises(TurnError) as caught:
            await operation

        assert caught.value.reason is TurnFailure.IDENTITY_MISMATCH
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"x": object()}, RpcFailure.INVALID_OUTBOUND),
        ({"x": float("nan")}, RpcFailure.INVALID_OUTBOUND),
        ({"x": "\ud800"}, RpcFailure.INVALID_OUTBOUND),
        ({"x": "a" * 301}, RpcFailure.FRAME_LIMIT),
    ],
)
@pytest.mark.asyncio
async def test_invalid_schema_snapshot_writes_no_turn_bytes_and_keeps_rpc_usable(
    schema: dict[str, object], reason: RpcFailure
) -> None:
    async with wire(frame_budget=300) as connection:
        with pytest.raises(RpcError) as caught:
            await observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                output_schema=schema,
            )

        await connection.rpc.notify("still-usable", deadline=future_deadline())
        request = await connection.peer_reader.readline()

        assert caught.value.reason is reason
        assert request == b'{"method":"still-usable"}\n'


@pytest.mark.asyncio
async def test_completed_message_then_eof_does_not_produce_a_terminal_candidate() -> (
    None
):
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1024)
        received = asyncio.Event()
        candidates: list[OutputCandidate] = []

        def observe(notice: Notification) -> None:
            state.accept(notice)
            received.set()

        async def project() -> None:
            result = await observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                output_schema=SCHEMA,
                observe=observe,
            )
            candidate = projected(result).output
            if candidate is not None:
                candidates.append(candidate)

        operation = connection.start(project())
        await connection.peer_reader.readline()

        await connection.send(
            started(),
            frame(
                {
                    "method": "item/completed",
                    "params": {"threadId": THREAD, "turnId": TURN, "item": item()},
                }
            ),
        )
        await received.wait()
        connection.peer_writer.write_eof()
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is RpcFailure.DISCONNECTED
        assert state.count == 1
        assert candidates == []
