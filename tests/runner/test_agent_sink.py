import asyncio
import json
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir.json import CanonicalBudget, JsonObject, spend_string
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.redaction import MASK
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import MAX_TEXT_BYTES
from kora.protocols.time import SystemClock
from kora.runner.journal import JournalAppendOutcome, RunningJournal
from kora.runner.sink import MAX_ERROR_EVENTS, MAX_RECORD_BYTES, QUEUE_RECORDS
from tests.runner.agent_support import (
    FailingWriter,
    HeldWriter,
    drained,
    private_directory,
    run,
    sink_for,
)

SUPERVISOR_KINDS: Final[tuple[str, ...]] = (
    "phase",
    "runner.started",
    "runner.candidate_started",
    "runner.outputs_sealed",
    "runner.quiescence",
    "runner.retired",
    "runner.journal_gap",
    "runner.cap_reached",
    "runner.session_spec",
    "runner.session_result",
    "runner.session_failed",
    "runtime.retired_without_runner",
    "transfer.materialised",
    "transfer.rejected",
    "control.attached",
    "control.detached",
    "control.auth_failed",
    "dispatcher.quiescence",
    "output",
    "command_exited",
)
ADMITTED_VALUE: Final[str] = "kora-dummy-secret-7f3a"
SHORT_VALUE: Final[str] = "k0r4s3cr"


def turn(text: str | None = None, message_id: str = "m1") -> JsonObject:
    data: JsonObject = {"message_id": message_id, "truncated": False}
    if text is not None:
        data["text"] = text
    return data


def journal_lines(root: Path) -> list[bytes]:
    return (root / "journal.jsonl").read_bytes().splitlines()


def journal_records(root: Path) -> list[JsonObject]:
    return [json.loads(line) for line in journal_lines(root)]


def canonical_text_size(text: str) -> int:
    budget = CanonicalBudget(MAX_TEXT_BYTES)
    spend_string(budget, text, "text", 0)
    return MAX_TEXT_BYTES - budget.remaining


def test_adapter_cannot_emit_supervisor_kinds(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())

    async def emitted() -> tuple[list[KoraProtocolError], tuple[bool, bool]]:
        sink = sink_for(journal)
        refusals: list[KoraProtocolError] = []
        for kind in SUPERVISOR_KINDS:
            with pytest.raises(KoraProtocolError) as raised:
                sink.emit(kind, {"omitted": 1})
            refusals.append(raised.value)
        sink.emit("turn", turn())
        return refusals, await drained(sink)

    refusals, complete = run(emitted())
    journal.close()

    assert {error.code for error in refusals} == {"event_kind_refused"}
    assert all(error.__cause__ is None for error in refusals)
    assert all(error.__suppress_context__ for error in refusals)
    assert all(error.details == {} for error in refusals)
    assert [record["kind"] for record in journal_records(tmp_path / "attempt")] == [
        "turn"
    ]
    assert complete == (True, True)


def test_journal_backpressure_never_blocks_held_writer() -> None:
    writer = HeldWriter()

    async def emitted() -> tuple[int, str | None, tuple[bool, bool]]:
        sink = sink_for(writer, max_events=QUEUE_RECORDS)
        sink.emit("turn", turn(message_id="m0"))
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for index in range(QUEUE_RECORDS + 100):
            sink.emit("turn", turn(message_id=f"m{index + 1}"))
        omitted = sink.omitted()
        cause = sink.enforcement().cause
        writer.gate.set()
        return omitted, cause, await drained(sink)

    omitted, cause, complete = run(emitted())

    kinds = [kind for kind, _ in writer.appended]
    assert omitted == 100
    assert cause == "max_events"
    assert kinds.count("turn") == QUEUE_RECORDS + 1
    assert kinds[-1] == "runner.journal_gap"
    assert writer.appended[-1][1] == {"omitted": 100}
    assert complete == (False, True)


def test_journal_backpressure_never_blocks_byte_bound() -> None:
    writer = HeldWriter()
    text = "x" * (MAX_TEXT_BYTES - 2)

    async def emitted() -> tuple[int, tuple[bool, bool]]:
        sink = sink_for(writer)
        sink.emit("turn", turn())
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for _ in range(80):
            sink.emit("turn", turn(text))
        omitted = sink.omitted()
        writer.gate.set()
        return omitted, await drained(sink)

    omitted, complete = run(emitted())

    turns = [kind for kind, _ in writer.appended if kind == "turn"]
    assert 0 < omitted < 80
    assert len(turns) + omitted == 81
    assert writer.appended[-1] == ("runner.journal_gap", {"omitted": omitted})
    assert complete == (False, True)


def test_journal_backpressure_never_blocks_failing_writer() -> None:
    writer = FailingWriter()

    async def emitted() -> tuple[str, str | None, int, tuple[bool, bool]]:
        sink = sink_for(writer)
        sink.emit("turn", turn())
        cause = await sink.termination()
        sink.emit("turn", turn(message_id="m2"))
        return cause, sink.enforcement().cause, sink.omitted(), await drained(sink)

    cause, enforced, omitted, complete = run(emitted())

    assert cause == "journal_unavailable"
    assert enforced == "journal_unavailable"
    assert omitted == 2
    assert writer.attempts == 1
    assert complete == (False, True)


def test_journal_backpressure_never_blocks_closed_journal(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    journal.close()

    async def emitted() -> str:
        sink = sink_for(journal)
        sink.emit("turn", turn())
        cause = await sink.termination()
        await drained(sink)
        return cause

    assert run(emitted()) == "journal_unavailable"


def test_journal_backpressure_never_blocks_journal_closed_after_fence(
    tmp_path: Path,
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())

    async def emitted() -> tuple[str | None, tuple[bool, bool]]:
        sink = sink_for(journal)
        sink.terminate("cancel")
        sink.fence()
        journal.close()
        sink.emit("turn", turn())
        complete = await drained(sink)
        return sink.cause(), complete

    cause, complete = run(emitted())

    assert cause == "cancel"
    assert complete == (False, True)


def test_event_content_is_bounded_and_classified_masking(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    kora_capture.scope.admit(ADMITTED_VALUE)
    kora_capture.scope.admit(SHORT_VALUE)
    registry = kora_capture.diagnostics.registry
    grown = SHORT_VALUE * ((MAX_TEXT_BYTES - 2) // len(SHORT_VALUE))
    escaped = "\x01" * 20_000

    async def emitted() -> tuple[bool, bool]:
        sink = sink_for(journal, registry)
        sink.emit("turn", turn(f"before {ADMITTED_VALUE} after"))
        sink.emit("turn", turn(grown, "m2"))
        sink.emit(
            "tool_result",
            {
                "tool_use_id": "t1",
                "is_error": False,
                "content": escaped,
                "truncated": False,
            },
        )
        sink.emit(
            "error",
            {"code": "harness_failed", "text": ADMITTED_VALUE, "truncated": False},
        )
        return await drained(sink)

    complete = run(emitted())
    journal.close()

    masked, regrown, cut, error = [
        record["data"] for record in journal_records(tmp_path / "attempt")
    ]
    assert isinstance(masked, dict)
    assert isinstance(regrown, dict)
    assert isinstance(cut, dict)
    assert isinstance(error, dict)
    assert masked["text"] == f"before {MASK} after"
    assert masked["truncated"] is False
    assert regrown["truncated"] is True
    assert isinstance(regrown["text"], str)
    assert regrown["text"].startswith(MASK)
    assert canonical_text_size(regrown["text"]) <= MAX_TEXT_BYTES
    assert cut["truncated"] is True
    assert isinstance(cut["content"], str)
    assert cut["content"] == "\x01" * ((MAX_TEXT_BYTES - 2) // 6)
    assert error["text"] == MASK
    assert (
        ADMITTED_VALUE.encode()
        not in (tmp_path / "attempt" / "journal.jsonl").read_bytes()
    )
    assert complete == (True, True)


def test_event_content_is_bounded_and_classified_record_bound(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    widest = '"' * 256
    text = '"' * ((MAX_TEXT_BYTES - 2) // 2)

    async def emitted() -> tuple[bool, bool]:
        sink = sink_for(journal)
        sink.emit(
            "turn",
            {
                "message_id": widest,
                "agent_id": widest,
                "text": text,
                "truncated": False,
            },
        )
        return await drained(sink)

    complete = run(emitted())
    journal.close()

    [line] = journal_lines(tmp_path / "attempt")
    [record] = journal_records(tmp_path / "attempt")
    assert len(line) <= MAX_RECORD_BYTES
    assert len(canonical_bytes(record)) <= MAX_RECORD_BYTES
    assert isinstance(record["data"], dict)
    assert record["data"]["text"] == text
    assert record["data"]["truncated"] is False
    assert complete == (True, True)


def test_event_content_is_bounded_and_classified_error_cap(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())

    async def emitted() -> tuple[str | None, tuple[bool, bool]]:
        sink = sink_for(journal)
        for _ in range(MAX_ERROR_EVENTS + 5):
            sink.emit("error", {"code": "harness_failed", "truncated": False})
        return sink.enforcement().cause, await drained(sink)

    cause, complete = run(emitted())
    journal.close()

    records = journal_records(tmp_path / "attempt")
    kinds = [record["kind"] for record in records]
    assert kinds.count("error") == MAX_ERROR_EVENTS
    assert records[-1]["kind"] == "runner.cap_reached"
    assert records[-1]["data"] == {"kind": "error_events", "omitted": 5}
    assert cause is None
    assert complete == (True, True)


def test_event_content_is_bounded_and_classified_invalid_event() -> None:
    writer = HeldWriter()
    writer.gate.set()

    async def emitted() -> KoraProtocolError:
        sink = sink_for(writer)
        with pytest.raises(KoraProtocolError) as raised:
            sink.emit("turn", {"message_id": "m1", "text": 7, "truncated": False})
        await drained(sink)
        return raised.value

    refusal = run(emitted())

    assert refusal.code == "event_refused"
    assert refusal.__cause__ is None
    assert writer.appended == []


def test_termination_survives_a_cancelled_waiter() -> None:
    writer = HeldWriter()
    writer.gate.set()

    async def waited() -> tuple[str, str]:
        sink = sink_for(writer)
        abandoned = asyncio.ensure_future(sink.termination())
        asyncio.get_running_loop().call_soon(abandoned.cancel)
        await asyncio.wait({abandoned})
        sink.terminate("cancel")
        sink.terminate("journal_unavailable")
        first = await sink.termination()
        second = await sink.termination()
        await drained(sink)
        return first, second

    assert run(waited()) == ("cancel", "cancel")


def test_journal_backpressure_never_blocks_absent_after_gap() -> None:
    writer = HeldWriter(JournalAppendOutcome.ABSENT)
    emitted_records = QUEUE_RECORDS + 4

    async def emitted() -> tuple[int, str, tuple[bool, bool]]:
        sink = sink_for(writer)
        sink.emit("turn", turn(message_id="m0"))
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for index in range(emitted_records - 1):
            sink.emit("turn", turn(message_id=f"m{index + 1}"))
        writer.gate.set()
        cause = await sink.termination()
        return sink.omitted(), cause, await drained(sink)

    omitted, cause, complete = run(emitted())

    assert omitted == emitted_records
    assert cause == "journal_unavailable"
    assert complete == (False, True)


def test_event_content_is_bounded_and_classified_straddling_secret(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    kora_capture.scope.admit(ADMITTED_VALUE)
    kept = MAX_TEXT_BYTES - 2
    prefix = "a" * (kept - 8)
    text = prefix + ADMITTED_VALUE + "b" * 20_000

    async def emitted() -> tuple[bool, bool]:
        sink = sink_for(journal, kora_capture.diagnostics.registry)
        sink.emit("turn", turn(text))
        return await drained(sink)

    complete = run(emitted())
    journal.close()

    [record] = journal_records(tmp_path / "attempt")
    assert record["data"] == {
        "message_id": "m1",
        "agent_id": None,
        "text": (prefix + MASK)[:kept],
        "truncated": True,
    }
    assert ADMITTED_VALUE[:8].encode() not in journal_lines(tmp_path / "attempt")[0]
    assert complete == (True, True)


def test_event_content_is_bounded_and_classified_dense_secret(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    dense = "s3cr" * 500
    kora_capture.scope.admit(dense)
    text = dense * 200 + " tail-content-xyz"

    async def emitted() -> tuple[bool, bool]:
        sink = sink_for(journal, kora_capture.diagnostics.registry)
        sink.emit("turn", turn(text))
        return await drained(sink)

    complete = run(emitted())
    journal.close()

    [record] = journal_records(tmp_path / "attempt")
    assert isinstance(record["data"], dict)
    assert record["data"]["truncated"] is True
    assert "tail-content-xyz" not in str(record["data"]["text"])
    assert dense not in str(record["data"]["text"])
    assert complete == (True, True)
