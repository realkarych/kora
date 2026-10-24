import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.measurements.lifetime_cohorts import (
    Cohort,
    Parent,
    ParentResult,
    cleanup_parents,
    run_cohorts,
)
from tests.measurements.lifetime_measurement_support import descriptor_kinds

CHILD: str = """
import asyncio
import sys
from pathlib import Path

async def main() -> None:
    reader, writer = await asyncio.open_unix_connection(sys.argv[1])
    writer.write(sys.argv[2].encode('ascii'))
    await writer.drain()
    try:
        request = await reader.readexactly(1)
        if request == b'f':
            raise SystemExit(3)
        await asyncio.get_running_loop().create_future()
    finally:
        writer.write(b'c')
        await writer.drain()
        if sys.argv[3] in ('cancel', 'partial'):
            await reader.read(1)
        Path(sys.argv[4]).write_text('closed')
        writer.close()
        await writer.wait_closed()

asyncio.run(main())
"""


@pytest.fixture
def control_directory() -> Iterator[Path]:
    with TemporaryDirectory(prefix="kora-cohorts-", dir="/tmp") as value:
        yield Path(value).resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier", ["failure", "cancel"])
async def test_measurement_supervisor_owns_exceptional_parent_exit(
    frontier: str, tmp_path: Path, control_directory: Path
) -> None:
    baseline_descriptors = descriptor_kinds()
    baseline_tasks = asyncio.all_tasks()
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    closing: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    connections: dict[int, asyncio.StreamWriter] = {}
    handlers: set[asyncio.Task[None]] = set()
    closed: set[int] = set()
    socket = control_directory / "control"

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            index = int(await reader.readexactly(1))
            connections[index] = writer
            if len(connections) == 3:
                ready.set_result(None)
            await reader.readexactly(1)
            closed.add(index)
            if len(closed) == 3:
                closing.set_result(None)
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.create_task(connected(reader, writer)))

    cohorts: list[Cohort] = []
    for index in range(3):
        folder = tmp_path / str(index)
        folder.mkdir(mode=0o700)
        home = folder / "home"
        home.mkdir(mode=0o700)
        cohorts.append(
            Cohort(
                (
                    sys.executable,
                    "-I",
                    "-c",
                    CHILD,
                    str(socket),
                    str(index),
                    frontier,
                    str(home / "cleanup"),
                ),
                home,
                folder,
            )
        )
    server = await asyncio.start_unix_server(accept, path=socket)
    running = asyncio.create_task(run_cohorts(tuple(cohorts), time.monotonic() + 600))
    try:
        async with asyncio.timeout(10):
            await ready

            if frontier == "cancel":
                running.cancel()
                await closing
                running.cancel()
                for writer in connections.values():
                    writer.write(b"x")
                    await writer.drain()
                with pytest.raises(asyncio.CancelledError):
                    await running
            else:
                connections[0].write(b"f")
                await connections[0].drain()
                with pytest.raises(RuntimeError, match="exited 3"):
                    await running

            for cohort in cohorts:
                result = ParentResult.model_validate_json(
                    (cohort.folder / "parent-exit.json").read_bytes()
                )
                assert result.reaped
                assert result.returncode is not None
                with pytest.raises(ChildProcessError):
                    os.waitpid(result.pid, os.WNOHANG)
                assert (cohort.home / "cleanup").read_text() == "closed"
                assert cohort.home.is_dir()
    finally:
        for writer in connections.values():
            writer.close()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)

    assert descriptor_kinds() == baseline_descriptors
    assert asyncio.all_tasks() == baseline_tasks


@pytest.mark.asyncio
async def test_partial_measurement_parent_cleanup_keeps_control_live(
    tmp_path: Path, control_directory: Path
) -> None:
    baseline_descriptors = descriptor_kinds()
    baseline_tasks = asyncio.all_tasks()
    ready: asyncio.Future[asyncio.StreamWriter] = (
        asyncio.get_running_loop().create_future()
    )
    closing: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    handlers: set[asyncio.Task[None]] = set()
    socket = control_directory / "partial"

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readexactly(1)
            ready.set_result(writer)
            await reader.readexactly(1)
            closing.set_result(None)
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.create_task(connected(reader, writer)))

    arguments = (
        sys.executable,
        "-I",
        "-c",
        CHILD,
        str(socket),
        "0",
        "partial",
        str(tmp_path / "cleanup"),
    )
    server = await asyncio.start_unix_server(accept, path=socket)
    parents: list[Parent] = []
    cleanup: asyncio.Task[None] | None = None
    writer = None
    final = time.monotonic() + 5
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        parents.append(Parent(Cohort(arguments, tmp_path, tmp_path), process))
        async with asyncio.timeout(10):
            writer = await ready

            cleanup = asyncio.create_task(cleanup_parents(parents, final))
            await closing

            assert process.returncode is None
            writer.write(b"x")
            await writer.drain()
            await cleanup
            assert process.returncode is not None
            assert process.returncode != -9
            with pytest.raises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
            assert (tmp_path / "cleanup").read_text() == "closed"
            assert tmp_path.is_dir()
    finally:
        if writer is not None:
            writer.close()
        if cleanup is None:
            cleanup = asyncio.create_task(cleanup_parents(parents, final))
        await asyncio.gather(cleanup, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)

    assert descriptor_kinds() == baseline_descriptors
    assert asyncio.all_tasks() == baseline_tasks
