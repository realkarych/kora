import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest

from kora.db.connection import Database, Lifecycle, parse_database_url
from kora.db.migrations import upgrade_database
from tests.api.serving_lifecycle_child import PRIVATE_LIFESPAN_REASON

SERVER_WAIT: float = 20.0


async def serving_event(
    stderr: asyncio.StreamReader, event_name: str
) -> dict[object, object]:
    while line := await stderr.readline():
        event: object = json.loads(line)
        if isinstance(event, dict):
            fields = cast("dict[object, object]", event)
            if fields.get("event") == event_name:
                return fields
    raise AssertionError(f"the child exited before {event_name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_serving_drains_after_sigterm_and_forced_sigint(
    tmp_path: Path, forced: bool
) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    await upgrade_database(settings)
    environment = dict(os.environ)
    environment.update(KORA_DATABASE_URL=str(settings.url), KORA_LOG_LEVEL="INFO")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.api.serving_lifecycle_child",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stderr is not None
    request_writer: asyncio.StreamWriter | None = None
    request_reader: asyncio.StreamReader | None = None
    response: bytes | None = None

    try:
        async with asyncio.timeout(SERVER_WAIT):
            bound = await serving_event(child.stderr, "api.serving_bound")
            port = bound.get("port")
            assert type(port) is int
            if forced:
                request_reader, request_writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                request_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await request_writer.drain()
                held = await child.stdout.readline()
                assert held == b"request_holds_sqlite\n"
            child.send_signal(signal.SIGTERM)
            await serving_event(child.stderr, "api.serving_draining")
            with pytest.raises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", port)
            if forced:
                child.send_signal(signal.SIGINT)
                assert request_reader is not None
                response = await request_reader.read()
            remaining, diagnostics = await child.communicate()
    finally:
        if request_writer is not None:
            request_writer.close()
            await request_writer.wait_closed()
        if child.returncode is None:
            child.kill()
            await child.wait()

    assert child.returncode == (130 if forced else 0), (remaining, diagnostics)
    assert b'"event":"api.serving_stopped"' in diagnostics
    assert b"drain_unfinished" not in diagnostics
    if forced:
        assert remaining == b"request_cancelled\n"
        assert response == b""
    else:
        assert remaining == b"lifespan_shutdown\n"


@pytest.mark.asyncio
async def test_serving_refused_lifespan_never_binds_or_leaks_reason(
    tmp_path: Path,
) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    await upgrade_database(settings)
    environment = dict(os.environ)
    environment.update(KORA_DATABASE_URL=str(settings.url), KORA_LOG_LEVEL="INFO")

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.api.serving_lifecycle_child",
        "refuse_lifespan",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(SERVER_WAIT):
            output, diagnostics = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    assert child.returncode == 70
    assert output == b""
    assert b'"reason":"startup_failed"' in diagnostics
    assert b"api.serving_bound" not in diagnostics
    assert PRIVATE_LIFESPAN_REASON.encode() not in diagnostics


@pytest.mark.asyncio
async def test_serving_keeps_the_original_process_until_held_native_work_settles(
    tmp_path: Path,
) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    await upgrade_database(settings)
    environment = dict(os.environ)
    environment.update(KORA_DATABASE_URL=str(settings.url), KORA_LOG_LEVEL="INFO")
    release_read, release_write = os.pipe()
    request_writer: asyncio.StreamWriter | None = None

    try:
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.api.serving_lifecycle_child",
                "hold_native",
                str(release_read),
                pass_fds=(release_read,),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(release_read)
        assert child.stdout is not None
        assert child.stderr is not None
        try:
            async with asyncio.timeout(SERVER_WAIT):
                bound = await serving_event(child.stderr, "api.serving_bound")
                port = bound.get("port")
                assert type(port) is int
                reader, request_writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                request_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await request_writer.drain()
                held = await child.stdout.readline()
                assert held == b"native_sqlite_held\n"
                child.send_signal(signal.SIGTERM)
                await serving_event(child.stderr, "api.serving_draining")
                child.send_signal(signal.SIGINT)
                original_wire = await reader.read()
                refusal = await serving_event(child.stderr, "api.serving_refused")
                still_owns_work = child.returncode is None
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection("127.0.0.1", port)
                os.write(release_write, b"r")
                output, diagnostics = await child.communicate()
        finally:
            if request_writer is not None:
                request_writer.close()
                await request_writer.wait_closed()
            if child.returncode is None:
                child.kill()
                await child.wait()
    finally:
        os.close(release_write)

    assert original_wire == b""
    assert refusal["reason"] == "drain_unfinished"
    assert still_owns_work
    assert child.returncode == 70
    assert output == b"native_sqlite_returned\nrequest_cancelled\n"
    assert b"api.serving_stopped" not in diagnostics


@pytest.mark.asyncio
async def test_sqlite_serving_close_waits_for_the_original_unit(tmp_path: Path) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    database = Database(settings, observe_sqlite_serving=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    close_started = asyncio.Event()

    async def read() -> None:
        async with database.read_transaction():
            entered.set()
            await release.wait()

    async def close() -> None:
        close_started.set()
        await database.close_sqlite_serving()

    async with asyncio.timeout(SERVER_WAIT), asyncio.TaskGroup() as tasks:
        tasks.create_task(read())
        await entered.wait()
        closing = tasks.create_task(close())
        await close_started.wait()
        assert database.lifecycle is Lifecycle.CLOSING
        assert not closing.done()
        release.set()

    assert database.lifecycle is Lifecycle.CLOSED
    assert database.units_in_flight() == 0
