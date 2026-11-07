import asyncio
import itertools
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kora.plugins.vcs.fake import store
from kora.plugins.vcs.fake.source import (
    DirInspectionSource,
    DirInspectionSources,
    chunk_at,
    reachable_sync,
    read_in,
)
from kora.protocols.errors import KoraProtocolError
from tests.plugins.vcs.dirvcs_checkout_support import empty_store, stored_revision


@pytest.mark.parametrize(
    "ending", ["cancel", "repeated_cancel", "worker_error", "cancelled_worker_error"]
)
def test_inspection_context_owns_active_read_until_completion(
    tmp_path: Path, ending: str
) -> None:
    path = tmp_path / "content"
    path.write_bytes(b"data")
    release = threading.Event()
    closed = threading.Event()
    baseline = len(os.listdir("/dev/fd"))

    async def exercise() -> None:
        entered = asyncio.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        source = DirInspectionSource(tmp_path)

        def operation() -> bytes:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(30):
                    raise TimeoutError
                if ending in ("worker_error", "cancelled_worker_error"):
                    raise OSError("read refused")
                return os.read(descriptor, 4)
            finally:
                os.close(descriptor)
                closed.set()

        async def owning_context() -> None:
            try:
                try:
                    await source.owner.run(operation)
                finally:
                    await source.owner.close()
            finally:
                finished.set()

        task = asyncio.create_task(owning_context())
        try:
            await asyncio.wait_for(entered.wait(), 30)
            with pytest.raises(KoraProtocolError):
                await source.index("content", 4)
            if ending != "worker_error":
                task.cancel()
                assert await asyncio.to_thread(source.owner.stopping.wait, 30)
                if ending == "repeated_cancel":
                    task.cancel()
                with pytest.raises(KoraProtocolError):
                    await source.index("content", 4)
            progressed = asyncio.Event()
            loop.call_soon(progressed.set)
            await asyncio.wait_for(progressed.wait(), 30)
            assert not finished.is_set()
            assert not closed.is_set()
        finally:
            release.set()
        if ending == "worker_error":
            with pytest.raises(OSError, match="read refused"):
                await asyncio.wait_for(task, 30)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 30)
        assert finished.is_set()
        assert source.owner.future is None

    asyncio.run(exercise())

    assert closed.is_set()
    assert len(os.listdir("/dev/fd")) == baseline


def test_inspection_source_normal_and_empty_close(tmp_path: Path) -> None:
    (tmp_path / "index").write_bytes(b"content")

    async def exercise() -> bytes:
        sources = DirInspectionSources()
        async with sources.open(str(tmp_path)) as source:
            found = await source.index("index", 7)
        async with sources.open(str(tmp_path)):
            pass
        with pytest.raises(KoraProtocolError):
            await source.index("index", 7)
        return found

    found = asyncio.run(exercise())

    assert found == b"content"


@pytest.mark.parametrize("selection", ["index", "blob"])
def test_inspection_filesystem_refusal_is_structured(
    tmp_path: Path, selection: str
) -> None:
    (tmp_path / "target").write_bytes(b"secret")
    blobs = tmp_path / "store" / "blobs"
    blobs.mkdir(parents=True)
    location = "a" * 64
    target = tmp_path / "index" if selection == "index" else blobs / location
    target.symlink_to(tmp_path / "target")

    async def exercise() -> None:
        async with DirInspectionSources().open(str(tmp_path)) as source:
            if selection == "index":
                await source.index("index", 100)
            else:
                async for _ in source.content(location, 100):
                    pass

    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(exercise())

    assert raised.value.code == "vcs_inspection_unavailable"


def test_cancelled_queued_read_never_opens_its_file(tmp_path: Path) -> None:
    release = threading.Event()
    created = tmp_path / "not-opened"

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        source = DirInspectionSource(tmp_path)
        entered = asyncio.Event()

        def occupying() -> None:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(30):
                raise TimeoutError

        def queued() -> None:
            created.write_bytes(b"unexpected")

        occupancy = loop.run_in_executor(None, occupying)
        await asyncio.wait_for(entered.wait(), 30)
        read = asyncio.create_task(source.owner.run(queued))
        progressed = asyncio.Event()
        loop.call_soon(progressed.set)
        await progressed.wait()
        try:
            assert source.owner.future is not None
            read.cancel()
            progressed.clear()
            loop.call_soon(progressed.set)
            await progressed.wait()
            assert source.owner.stopping.is_set()
            assert not read.done()
        finally:
            release.set()
        await asyncio.wait_for(occupancy, 30)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 30)
        await source.owner.close()

    asyncio.run(exercise())

    assert not created.exists()


def test_loop_shutdown_waits_for_the_active_read(tmp_path: Path) -> None:
    closed = threading.Event()
    observed: list[bool] = []

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        source = DirInspectionSource(tmp_path)

        def operation() -> None:
            loop.call_soon_threadsafe(entered.set)
            assert source.owner.stopping.wait(30)
            observed.append(closed.wait(0.2))

        async def holder() -> None:
            try:
                await source.owner.run(operation)
            finally:
                await source.owner.close()
                closed.set()

        asyncio.create_task(holder())
        await asyncio.wait_for(entered.wait(), 30)

    asyncio.run(exercise())

    assert observed == [False]
    assert closed.is_set()


def stops_at(call: int) -> Callable[[], bool]:
    calls = itertools.count(1)
    return lambda: next(calls) >= call


@pytest.mark.parametrize("call", [1, 2, 3])
def test_real_read_stops_at_each_cooperative_check(tmp_path: Path, call: int) -> None:
    (tmp_path / "large").write_bytes(b"x" * (2 * store.MIB))
    baseline = len(os.listdir("/dev/fd"))

    with pytest.raises(store.Failed):
        read_in(tmp_path, "large", 4 * store.MIB, stops_at(call))

    assert len(os.listdir("/dev/fd")) == baseline


@pytest.mark.parametrize("call", [1, 2, 3])
def test_real_chunk_stops_at_each_cooperative_check(tmp_path: Path, call: int) -> None:
    prepared = tmp_path / store.PREPARED_STORE
    empty_store(prepared)
    location = store.digest_of(b"data")
    (prepared / "blobs" / location).write_bytes(b"data")
    baseline = len(os.listdir("/dev/fd"))

    with pytest.raises(store.Failed):
        chunk_at(tmp_path, location, 0, 4, stops_at(call))

    assert len(os.listdir("/dev/fd")) == baseline


def test_real_reachability_stops_when_cancelled(tmp_path: Path) -> None:
    prepared = tmp_path / store.PREPARED_STORE
    empty_store(prepared)
    base = stored_revision(prepared, {"a": (store.REGULAR, b"a")}, None, "base")
    revision = stored_revision(prepared, {"a": (store.REGULAR, b"b")}, base, "new")
    cancelled = threading.Event()
    cancelled.set()
    baseline = len(os.listdir("/dev/fd"))

    with pytest.raises(store.Failed):
        reachable_sync(tmp_path, revision, base, cancelled)

    assert len(os.listdir("/dev/fd")) == baseline


@pytest.mark.parametrize("operation", ["index", "content", "revision"])
def test_read_stopped_before_it_starts_is_a_structured_refusal(
    tmp_path: Path, operation: str
) -> None:
    release = threading.Event()

    async def read(source: DirInspectionSource) -> None:
        if operation == "index":
            await source.index("index", 100)
        elif operation == "revision":
            await source.revision_bytes("a" * 64, 100)
        else:
            async for _ in source.content("a" * 64, 100):
                pass

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        source = DirInspectionSource(tmp_path)
        occupied = asyncio.Event()

        def occupying() -> None:
            loop.call_soon_threadsafe(occupied.set)
            if not release.wait(30):
                raise TimeoutError

        occupancy = loop.run_in_executor(None, occupying)
        await asyncio.wait_for(occupied.wait(), 30)
        reading = asyncio.create_task(read(source))
        progressed = asyncio.Event()
        loop.call_soon(progressed.set)
        await progressed.wait()
        source.owner.stopping.set()
        release.set()
        await asyncio.wait_for(occupancy, 30)
        await asyncio.wait_for(reading, 30)

    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(exercise())

    assert raised.value.code == "vcs_inspection_unavailable"
