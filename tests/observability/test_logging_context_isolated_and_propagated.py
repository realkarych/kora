import asyncio

import pytest

from kora.observability import (
    ContextError,
    Level,
    LoggingSettings,
    LossReason,
    bound_scope,
    current_context,
)
from tests.observability.conftest import (
    LEVEL_EVENTS,
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
)


@pytest.mark.asyncio
async def test_concurrent_tasks_see_only_their_own_context(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()
    gate = asyncio.Event()

    async def work(run_id: str, attempt_id: str) -> None:
        with bound_scope(run_id=run_id, attempt_id=attempt_id):
            await gate.wait()
            log.emit(NOTE, text=run_id)

    tasks = [
        asyncio.create_task(work(f"run-{index}", f"att-{index}")) for index in range(5)
    ]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    pipe.reader.wait_for_lines(5)

    records = pipe.reader.records()
    assert {record["run_id"] for record in records} == {f"run-{i}" for i in range(5)}
    assert all(record["text"] == record["run_id"] for record in records)
    assert all(
        record["attempt_id"] == str(record["run_id"]).replace("run", "att")
        for record in records
    )
    assert current_context() == {}


@pytest.mark.asyncio
async def test_thread_bridge_carries_the_copied_context(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    with bound_scope(run_id="run-thread", lease_epoch=3):
        await asyncio.to_thread(log.emit, NOTE, text="from-thread")
    pipe.reader.wait_for_lines(1)

    record = pipe.reader.records()[0]
    assert record["run_id"] == "run-thread"
    assert record["lease_epoch"] == 3
    assert record["text"] == "from-thread"


@pytest.mark.asyncio
async def test_context_resets_after_exit_exception_and_cancellation() -> None:
    with bound_scope(run_id="run-exit"):
        assert current_context() == {"run_id": "run-exit"}
    assert current_context() == {}

    with pytest.raises(ValueError), bound_scope(run_id="run-error"):
        raise ValueError("expected")
    assert current_context() == {}

    seen: list[dict[str, str | int]] = []

    async def cancelled_work() -> None:
        try:
            with bound_scope(run_id="run-cancelled"):
                await asyncio.sleep(60)
        finally:
            seen.append(current_context())

    task = asyncio.create_task(cancelled_work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [{}]
    assert current_context() == {}


def test_logger_bind_carries_validated_context(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT).bind(namespace_id="default")
    pipe.reader.start()

    log.emit(NOTE, text="bound")
    pipe.reader.wait_for_lines(1)

    assert pipe.reader.records()[0]["namespace_id"] == "default"
    with pytest.raises(ContextError):
        log.bind(run_id="not valid")
    with pytest.raises(ContextError):
        log.bind(lease_epoch=-1)


def test_event_field_cannot_override_bound_context(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    with bound_scope(run_id="run-real"):
        log.emit(NOTE, **{"text": "forged", "run_id": "run-forged"})
        log.emit(LEVEL_EVENTS[Level.INFO], text="after")
    diagnostics.shutdown()
    pipe.close_write_end()
    pipe.reader.wait_closed()

    records = pipe.reader.records()
    events = [record["event"] for record in records]
    rejected = diagnostics.snapshot().rejected
    assert "run-forged" not in "".join(pipe.reader.lines())
    assert events == ["logging.record_rejected", "testing.info", "logging.loss_summary"]
    assert records[0]["reason"] == LossReason.CONTEXT_OVERRIDE.value
    assert records[0]["source_component"] == TEST_COMPONENT
    assert records[1]["run_id"] == "run-real"
    assert rejected == {(TEST_COMPONENT, "INFO", "context_override"): 1}


def test_malformed_identifier_is_refused_at_bind_time() -> None:
    with pytest.raises(ContextError):
        with bound_scope(run_id="has space"):
            pass
    with pytest.raises(ContextError):
        with bound_scope(unknown_key="value"):
            pass
    with pytest.raises(ContextError):
        with bound_scope(run_id="x" * 257):
            pass
    assert current_context() == {}
