import asyncio
import gc
import traceback
import weakref
from typing import cast

import pytest

from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import (
    AppServerTurnSession,
    TurnObservations,
)
from kora.plugins.harness.codex.app_server_tools import (
    CommandSource,
    ToolCounts,
    ToolFailure,
    ToolKind,
    ToolPhase,
    ToolRefused,
    ToolStatus,
    TurnTools,
    decode_tool,
)
from tests.plugins.harness.codex.test_app_server_messages import encoded
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
)
from tests.plugins.harness.codex.test_app_server_usage import (
    FIRST,
    snapshot,
    usage_frame,
)
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

ABSENT: object = object()
PRIVATE = "private-tool-observation-content"
ITEM = "selected-tool-item"


def tool_notice(
    *,
    phase: ToolPhase = "started",
    kind: object = "commandExecution",
    status: object = "inProgress",
    identity: object = ITEM,
    thread: object = THREAD,
    turn: object = TURN,
    source: object = ABSENT,
    exit_code: object = ABSENT,
) -> Notification:
    item: dict[str, object] = {
        "type": kind,
        "id": identity,
        "status": status,
        "command": PRIVATE,
        "cwd": PRIVATE,
        "aggregatedOutput": PRIVATE,
        "modelContext": {"model": PRIVATE},
        "changes": [{"path": PRIVATE, "diff": PRIVATE}],
    }
    if source is not ABSENT:
        item["source"] = source
    if exit_code is not ABSENT:
        item["exitCode"] = exit_code
    return Notification(
        "item/" + phase,
        {"threadId": thread, "turnId": turn, "item": item},
        None,
    )


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange"])
@pytest.mark.parametrize("phase", ["started", "completed"])
@pytest.mark.parametrize("status", ["inProgress", "completed", "failed", "declined"])
def test_projection_keeps_notification_phase_independent_from_native_status(
    kind: ToolKind, phase: ToolPhase, status: ToolStatus
) -> None:
    notice = tool_notice(kind=kind, phase=phase, status=status)

    observation = decode_tool(notice)

    assert observation is not None
    assert (observation.thread_id, observation.turn_id, observation.item_id) == (
        THREAD,
        TURN,
        ITEM,
    )
    assert (observation.kind, observation.phase, observation.status) == (
        kind,
        phase,
        status,
    )
    assert observation.source is None
    assert observation.exit_code is None
    assert PRIVATE not in repr(observation)
    assert ITEM not in repr(observation)
    assert not hasattr(observation, "command")
    assert not hasattr(observation, "changes")
    assert not hasattr(observation, "aggregated_output")


@pytest.mark.parametrize(
    "source", ["agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction"]
)
@pytest.mark.parametrize("exit_code", [None, -(2**31), -1, 0, 2**31 - 1])
def test_command_metadata_preserves_closed_source_and_nullable_int32_exit_code(
    source: CommandSource, exit_code: int | None
) -> None:
    notice = tool_notice(source=source, exit_code=exit_code, phase="completed")

    observation = decode_tool(notice)

    assert observation is not None
    assert observation.source == source
    assert observation.exit_code == exit_code
    assert observation.status == "inProgress"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", None),
        ("status", PRIVATE),
        ("status", True),
        ("status", []),
        ("source", None),
        ("source", PRIVATE),
        ("source", True),
        ("source", {}),
        ("exit_code", True),
        ("exit_code", 0.0),
        ("exit_code", PRIVATE),
        ("exit_code", -(2**31) - 1),
        ("exit_code", 2**31),
        ("identity", ""),
        ("identity", None),
        ("identity", True),
        ("identity", "x" * 257),
        ("identity", "é" * 129),
        ("identity", "\ud800"),
        ("thread", ""),
        ("turn", ""),
        ("kind", None),
        ("kind", []),
        ("kind", ""),
    ],
    ids=[
        "null-status",
        "unknown-status",
        "boolean-status",
        "list-status",
        "null-source",
        "unknown-source",
        "boolean-source",
        "object-source",
        "boolean-exit",
        "float-exit",
        "text-exit",
        "exit-underflow",
        "exit-overflow",
        "empty-id",
        "null-id",
        "boolean-id",
        "long-id",
        "wide-id",
        "surrogate-id",
        "empty-thread",
        "empty-turn",
        "null-kind",
        "list-kind",
        "empty-kind",
    ],
)
def test_malformed_owned_metadata_has_only_a_fixed_content_free_refusal(
    field: str, value: object
) -> None:
    notice = tool_notice()
    params = cast("dict[str, object]", notice.params)
    item = cast("dict[str, object]", params["item"])
    if field in ("thread", "turn"):
        params[field + "Id"] = value
    else:
        name = {"identity": "id", "exit_code": "exitCode", "kind": "type"}.get(
            field, field
        )
        item[name] = value

    with pytest.raises(ToolRefused) as caught:
        decode_tool(notice)

    assert caught.value.reason is ToolFailure.INVALID_OBSERVATION
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "identity", ["x" * 256, "é" * 128, "🌲" * 64], ids=["ascii", "wide", "astral"]
)
def test_identity_admits_the_existing_256_utf8_byte_boundary(identity: str) -> None:
    notice = tool_notice(identity=identity)

    observation = decode_tool(notice)

    assert observation is not None
    assert observation.item_id == identity


@pytest.mark.parametrize(
    "method",
    ["turn/completed", "item/commandExecution/outputDelta", "item/agentMessage/delta"],
)
def test_unowned_methods_do_not_parse_or_retain_payloads(method: str) -> None:
    notification = Notification(method, PRIVATE, None)

    observation = decode_tool(notification)

    assert observation is None


@pytest.mark.parametrize(
    "kind", ["agentMessage", "mcpToolCall", "collabAgentToolCall", "future-kind"]
)
def test_other_item_kinds_do_not_become_shell_or_file_edit_observations(
    kind: str,
) -> None:
    notification = tool_notice(kind=kind, identity=None, status=None)

    observation = decode_tool(notification)

    assert observation is None


def test_unowned_file_change_command_fields_cannot_supply_metadata() -> None:
    notice = tool_notice(kind="fileChange", source=PRIVATE, exit_code=True)

    observation = decode_tool(notice)

    assert observation is not None
    assert observation.source is None
    assert observation.exit_code is None


@pytest.mark.parametrize("params", [None, PRIVATE, [], {}, {"item": None}])
def test_malformed_owned_envelopes_refuse(params: object) -> None:
    notice = Notification("item/started", params, None)

    with pytest.raises(ToolRefused) as caught:
        decode_tool(notice)

    assert caught.value.reason is ToolFailure.INVALID_OBSERVATION


def test_interleaved_lifecycles_count_notifications_without_inferred_completion() -> (
    None
):
    tools = TurnTools(THREAD, max_tools=3)
    sequence = (
        tool_notice(status="completed"),
        tool_notice(identity="second", kind="fileChange"),
        tool_notice(phase="completed", status="failed"),
        tool_notice(identity="orphan", phase="completed", status="inProgress"),
        tool_notice(
            identity="second", kind="fileChange", phase="completed", status="declined"
        ),
    )

    counts: list[ToolCounts] = []
    for notice in sequence:
        tools.accept(notice)
        counts.append(tools.counts)
    latest = tools.for_turn(TURN)

    assert counts == [
        ToolCounts(1, 1, 0, 1, 0),
        ToolCounts(2, 2, 0, 2, 0),
        ToolCounts(2, 2, 1, 1, 0),
        ToolCounts(3, 2, 2, 1, 1),
        ToolCounts(3, 2, 3, 0, 1),
    ]
    assert all(
        c.completed == c.started - c.open + c.completion_without_start for c in counts
    )
    assert latest is not None
    assert latest.kind == "fileChange"
    assert latest.status == "declined"


@pytest.mark.parametrize(
    ("first_completed", "phase", "kind", "expected"),
    [
        (False, "started", "commandExecution", ToolFailure.DUPLICATE),
        (True, "completed", "commandExecution", ToolFailure.DUPLICATE),
        (True, "started", "commandExecution", ToolFailure.INVALID_TRANSITION),
        (False, "completed", "fileChange", ToolFailure.INVALID_TRANSITION),
    ],
)
def test_duplicate_and_invalid_transitions_preserve_state(
    first_completed: bool, phase: ToolPhase, kind: ToolKind, expected: ToolFailure
) -> None:
    tools = TurnTools(THREAD, max_tools=1)
    first = tool_notice(phase="completed" if first_completed else "started")
    latest = tools.accept(first)
    before = tools.counts

    with pytest.raises(ToolRefused) as caught:
        tools.accept(tool_notice(phase=phase, kind=kind))

    assert caught.value.reason is expected
    assert tools.counts == before
    assert tools.for_turn(TURN) is latest


@pytest.mark.parametrize("wrong_thread", [False, True])
def test_cross_identity_refusal_preserves_selected_state(wrong_thread: bool) -> None:
    tools = TurnTools(THREAD, max_tools=2)
    latest = tools.accept(tool_notice())
    previous = tools.counts
    notice = tool_notice(
        identity="new",
        thread="wrong" if wrong_thread else THREAD,
        turn=TURN if wrong_thread else "wrong",
    )

    with pytest.raises(ToolRefused) as caught:
        tools.accept(notice)

    assert caught.value.reason is ToolFailure.IDENTITY_MISMATCH
    assert tools.counts == previous
    assert tools.for_turn(TURN) is latest


def test_full_admission_still_accepts_completion_and_remembers_completed_ids() -> None:
    tools = TurnTools(THREAD, max_tools=1)
    tools.accept(tool_notice())
    previous = tools.counts

    with pytest.raises(ToolRefused) as overflow:
        tools.accept(tool_notice(identity="new"))
    tools.accept(tool_notice(phase="completed"))
    with pytest.raises(ToolRefused) as duplicate:
        tools.accept(tool_notice(phase="completed"))
    latest = tools.for_turn(TURN)

    assert previous == ToolCounts(1, 1, 0, 1, 0)
    assert overflow.value.reason is ToolFailure.ITEM_LIMIT
    assert duplicate.value.reason is ToolFailure.DUPLICATE
    assert tools.counts == ToolCounts(1, 1, 1, 0, 0)
    assert latest is not None
    assert latest.phase == "completed"


def test_reducer_releases_replaced_observations_without_copying_content() -> None:
    tools = TurnTools(THREAD, max_tools=2)
    previous = tools.accept(tool_notice(identity=PRIVATE))
    assert previous is not None
    reference = weakref.ref(previous)

    latest = tools.accept(tool_notice(identity="replacement"))
    del previous
    gc.collect()

    assert reference() is None
    assert tools.for_turn(TURN) is latest
    assert PRIVATE not in repr(latest)


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_session_confirms_tool_state_only_after_matching_start_reply(
    early: bool,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=2)
        observed: list[TurnObservations] = []
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.append(session.snapshot()),
            )
        )
        await connection.peer_reader.readline()
        notices = (encoded(tool_notice()), encoded(tool_notice(phase="completed")))
        frames = (*notices, started()) if early else (started(), *notices)

        await connection.send(*frames, completed())
        await operation
        state = session.snapshot()

        assert state.tool_counts == ToolCounts(1, 1, 1, 0, 0)
        assert state.latest_tool is not None
        assert state.latest_tool.phase == "completed"
        assert state.latest_tool.item_id == ITEM
        assert state.turn_id == TURN
        assert state.terminal_status == "completed"
        assert state.final_message is None
        if early:
            assert observed == [TurnObservations(), TurnObservations()]
        else:
            assert observed[0].tool_counts == ToolCounts(1, 1, 0, 1, 0)
        assert owned_tasks() == set()


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_wrong_tool_turn_never_replaces_confirmed_state(early: bool) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=2)
        observed = asyncio.Event()
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.set(),
            )
        )
        await connection.peer_reader.readline()
        if not early:
            await connection.send(started(), usage_frame())
            await observed.wait()
        previous = session.snapshot()
        bad = encoded(tool_notice(turn="wrong"))

        await connection.send(bad, started()) if early else await connection.send(bad)
        with pytest.raises(ToolRefused) as caught:
            await operation

        assert caught.value.reason is ToolFailure.IDENTITY_MISMATCH
        assert session.snapshot() is previous
        assert previous.tool_counts == ToolCounts()
        assert previous.usage == (None if early else snapshot(FIRST))
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_presend_tool_lookahead_refuses_without_sending_turn_bytes() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=1)
        setup = connection.start(
            connection.rpc.call(
                "fixture",
                {},
                deadline=future_deadline(),
                observe=lambda _notice: None,
            )
        )
        await connection.peer_reader.readline()
        payload = encoded(tool_notice())
        await connection.send(frame({"id": 1, "result": {}}), payload[:13])
        await setup
        await connection.send(payload[13:])

        with pytest.raises(ToolRefused) as caught:
            await session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is ToolFailure.PREMATURE
        assert session.snapshot() == TurnObservations()
        assert remaining == b""
        assert owned_tasks() == set()


@pytest.mark.parametrize("ending", ["eof", "cancel", "interrupt"])
@pytest.mark.asyncio
async def test_partial_tool_lifecycle_survives_failure_and_native_interruption(
    ending: str,
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=1)
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
        await connection.peer_reader.readline()
        await connection.send(started(), encoded(tool_notice(status="completed")))
        await observed.wait()
        previous = session.snapshot()

        if ending == "eof":
            connection.peer_writer.close()
            await connection.peer_writer.wait_closed()
            with pytest.raises(RpcError) as caught:
                await operation
            assert caught.value.reason is RpcFailure.DISCONNECTED
        elif ending == "cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            stop.set()
            await interrupt_request(connection)
            await connection.send(
                frame({"id": 2, "result": {}}), completed(status="interrupted")
            )
            await operation
        state = session.snapshot()

        assert state.tool_counts == previous.tool_counts == ToolCounts(1, 1, 0, 1, 0)
        assert state.latest_tool is previous.latest_tool
        assert state.terminal_status == (
            "interrupted" if ending == "interrupt" else None
        )
        assert state.final_message is None
        assert owned_tasks() == set()


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        ("malformed", ToolFailure.INVALID_OBSERVATION),
        ("duplicate", ToolFailure.DUPLICATE),
        ("kind-change", ToolFailure.INVALID_TRANSITION),
        ("overflow", ToolFailure.ITEM_LIMIT),
    ],
)
@pytest.mark.asyncio
async def test_refused_tool_input_keeps_the_previous_session_snapshot(
    ending: str, expected: ToolFailure
) -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=1)
        observed = asyncio.Event()
        seen: list[Notification] = []

        def observe(notification: Notification) -> None:
            seen.append(notification)
            if len(seen) == 2:
                observed.set()

        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=observe,
            )
        )
        await connection.peer_reader.readline()
        await connection.send(started(), usage_frame(), encoded(tool_notice()))
        await observed.wait()
        previous = session.snapshot()
        refused = {
            "malformed": tool_notice(phase="completed", source=PRIVATE),
            "duplicate": tool_notice(),
            "kind-change": tool_notice(phase="completed", kind="fileChange"),
            "overflow": tool_notice(identity="second"),
        }[ending]

        await connection.send(encoded(refused))
        with pytest.raises(ToolRefused) as caught:
            await operation

        assert caught.value.reason is expected
        assert session.snapshot() is previous
        assert previous.tool_counts == ToolCounts(1, 1, 0, 1, 0)
        assert previous.usage == snapshot(FIRST)
        assert previous.latest_tool is not None
        assert previous.latest_tool.item_id == ITEM
        assert len(seen) == 2
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_session_releases_replaced_tool_observations() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=1, max_tools=2)
        observed = asyncio.Event()
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.set(),
            )
        )
        await connection.peer_reader.readline()
        await connection.send(started(), encoded(tool_notice(identity=PRIVATE)))
        await observed.wait()
        previous = session.snapshot()
        assert previous.latest_tool is not None
        reference = weakref.ref(previous.latest_tool)
        observed.clear()

        await connection.send(encoded(tool_notice(identity="replacement")))
        await observed.wait()
        del previous
        gc.collect()
        await connection.send(completed())
        await operation
        current = session.snapshot()

        assert reference() is None
        assert current.tool_counts == ToolCounts(2, 2, 0, 2, 0)
        assert current.latest_tool is not None
        assert current.latest_tool.item_id == "replacement"
        assert PRIVATE not in repr(current)
        assert current.terminal_status == "completed"
        assert owned_tasks() == set()
