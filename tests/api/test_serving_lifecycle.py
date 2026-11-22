import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest

from kora.db.connection import (
    Database,
    DatabaseClosedError,
    Lifecycle,
    parse_database_url,
)
from kora.db.migrations import upgrade_database
from tests.api.serving_lifecycle_child import PRIVATE_LIFESPAN_REASON

SERVER_WAIT: float = 20.0


async def bound_port(stderr: asyncio.StreamReader) -> int:
    while line := await stderr.readline():
        event: object = json.loads(line)
        if isinstance(event, dict):
            fields = cast("dict[object, object]", event)
            port = fields.get("port")
            if fields.get("event") == "api.serving_bound" and type(port) is int:
                return port
    raise AssertionError("the child exited before binding")


async def listener_closed(port: int) -> None:
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except ConnectionRefusedError:
            return
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.01)


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

    try:
        async with asyncio.timeout(SERVER_WAIT):
            port = await bound_port(child.stderr)
            if forced:
                _, request_writer = await asyncio.open_connection("127.0.0.1", port)
                request_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await request_writer.drain()
                held = await child.stdout.readline()
                assert held == b"request_holds_sqlite\n"
            child.send_signal(signal.SIGTERM)
            await listener_closed(port)
            if forced:
                child.send_signal(signal.SIGINT)
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
async def test_sqlite_serving_close_reports_the_original_held_unit(
    tmp_path: Path,
) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    database = Database(settings)

    immediate: bool | None = None
    with pytest.raises(DatabaseClosedError, match="before the unit finished"):
        async with database.read_transaction():
            immediate = await database.close_sqlite_serving(0)
    after_unit_release = await database.close_sqlite_serving()

    assert immediate is False
    assert after_unit_release is True
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_sqlite_serving_close_waits_for_the_original_unit(tmp_path: Path) -> None:
    settings = parse_database_url(f"sqlite+aiosqlite:///{tmp_path / 'metadata.sqlite'}")
    database = Database(settings)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def read() -> None:
        async with database.read_transaction():
            entered.set()
            await release.wait()

    async with asyncio.timeout(SERVER_WAIT), asyncio.TaskGroup() as tasks:
        tasks.create_task(read())
        await entered.wait()
        closing = tasks.create_task(database.close_sqlite_serving())
        while database.lifecycle is Lifecycle.OPEN:
            await asyncio.sleep(0)
        release.set()

    assert closing.result() is True
    assert database.units_in_flight() == 0
