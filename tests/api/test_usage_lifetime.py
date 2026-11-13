import asyncio
from dataclasses import dataclass, field
from types import CoroutineType
from typing import cast

import pytest
from starlette.types import Message

from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect, Lifecycle
from kora.engine.locks import TransitionLocks
from tests.api.support import HttpCall, api_capture
from tests.api.usage_support import report, usage_application, usage_path
from tests.db.closing import ConnectionLedger, until
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter
from tests.engine.worker_usage_support import UsageCase, usage_case, usage_snapshot

__all__ = ["api_capture"]


@dataclass
class UsageCall(HttpCall):
    payload: bytes = b""
    pause: str = "none"
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> Message:
        self.receives += 1
        if self.pause == "body":
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), DEADLINE)
        return {"type": "http.request", "body": self.payload, "more_body": False}

    async def send(self, message: Message) -> None:
        if self.pause == "response" and message["type"] == "http.response.start":
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), DEADLINE)
        await super().send(message)


def call_for(case: UsageCase, pause: str = "none") -> UsageCall:
    return UsageCall(
        path=usage_path(case),
        method="POST",
        headers=[
            (b"authorization", ("Bearer " + case.bearer_token).encode()),
            (b"content-type", b"application/json"),
        ],
        payload=report(case).model_dump_json().encode(),
        pause=pause,
    )


async def hold_run(
    database: Database, case: UsageCase, ready: asyncio.Event, release: asyncio.Event
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(case.context.run_id)
        ready.set()
        await asyncio.wait_for(release.wait(), DEADLINE)


def protected_writer_waits(
    database: Database, before: set[asyncio.Task[object]]
) -> bool:
    found = 0
    for task in asyncio.all_tasks() - before:
        coroutine = task.get_coro()
        if (
            isinstance(coroutine, CoroutineType)
            and coroutine.cr_code.co_filename == Database.run.__code__.co_filename
            and coroutine.cr_code.co_qualname == "Database.run.<locals>.execute"
            and coroutine.cr_frame is not None
            and coroutine.cr_frame.f_locals.get("self") is database
            and coroutine.cr_await is not None
            and not task.done()
        ):
            found += 1
    assert found <= 1
    return found == 1


async def wait_for_usage_writer(
    database: Database, ledger: ConnectionLedger, before: set[asyncio.Task[object]]
) -> None:
    if database.dialect is Dialect.SQLITE:
        await until(lambda: protected_writer_waits(database, before))
        assert database.units_in_flight() == 2
        assert ledger.outstanding == 1
    else:
        await wait_for_lock_waiter(database)


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["body", "response"])
async def test_usage_http_cancellation_does_not_retain_database_resources(
    database: Database, api_capture: Capture, pause: str
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    call = call_for(case, pause)
    ledger = ConnectionLedger(database)
    before = await usage_snapshot(database, case)
    task = asyncio.create_task(call.run(app))

    try:
        await asyncio.wait_for(call.reached.wait(), DEADLINE)
        assert ledger.outstanding == database.units_in_flight() == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    finally:
        call.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    after = await usage_snapshot(database, case)
    following = call_for(case)
    await following.run(app)

    assert (after == before) is (pause == "body")
    assert following.status == 200
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
async def test_usage_admitted_writer_settles_before_cancellation_or_shutdown(
    database: Database, api_capture: Capture, ending: str
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    call = call_for(case)
    ready, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)
    holder = asyncio.create_task(hold_run(database, case, ready, release))
    await asyncio.wait_for(ready.wait(), DEADLINE)
    before: set[asyncio.Task[object]] = set(asyncio.all_tasks())
    writer = asyncio.create_task(call.run(app))
    closing: asyncio.Task[None] | None = None

    try:
        await wait_for_usage_writer(database, ledger, before)
        if ending == "cancel":
            writer.cancel()
        else:
            closing = asyncio.create_task(database.close(deadline_seconds=DEADLINE))
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
        release.set()
        await asyncio.wait_for(holder, DEADLINE)
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(writer, DEADLINE)
        else:
            await asyncio.wait_for(writer, DEADLINE)
            assert closing is not None
            await asyncio.wait_for(closing, DEADLINE)
    finally:
        release.set()
        await asyncio.gather(holder, writer, return_exceptions=True)
        if closing is not None:
            await closing
    reopened = Database(database.settings)
    try:
        saved = await usage_snapshot(reopened, case)
    finally:
        await reopened.close()

    assert isinstance(saved[0], tuple)
    rows = cast("tuple[object, ...]", saved[0])
    assert len(rows) == 1
    assert saved[2] is True
    assert ledger.outstanding == database.units_in_flight() == 0
    if ending == "shutdown":
        assert call.status == 200
        assert database.lifecycle is Lifecycle.CLOSED
    else:
        following = call_for(case)
        await following.run(app)
        assert following.status == 200
        assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_usage_signal_lock_timeout_preserves_sqlite_queue_semantics(
    database: Database, api_capture: Capture
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case, lock_timeout_ms=200)
    ready, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)
    before = await usage_snapshot(database, case)
    holder = asyncio.create_task(hold_run(database, case, ready, release))
    await asyncio.wait_for(ready.wait(), DEADLINE)
    call = call_for(case)
    baseline: set[asyncio.Task[object]] = set(asyncio.all_tasks())
    writer = asyncio.create_task(call.run(app))

    try:
        await wait_for_usage_writer(database, ledger, baseline)
        if database.dialect is Dialect.SQLITE:
            release.set()
        await asyncio.wait_for(writer, DEADLINE)
    finally:
        release.set()
        await asyncio.gather(holder, writer, return_exceptions=True)
    after = await usage_snapshot(database, case)
    following = call_for(case)
    await following.run(app)

    if database.dialect is Dialect.POSTGRESQL:
        assert call.status == 409
        assert b'"reason":"lock_timeout"' in call.body
        assert b'"retryable":true' in call.body
        assert before == after
    else:
        assert call.status == 200
        assert before != after
    assert following.status == 200
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []
