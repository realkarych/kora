import asyncio
import ssl

import pytest

from tests.plugins.harness.codex.selected_tls import UnknownCaAlert


@pytest.mark.asyncio
async def test_tls_observer_restores_threshold_after_debug_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, asyncio.BaseEventLoop)
    original_handler = loop.get_exception_handler()
    original_debug = loop.get_debug()
    original_threshold = loop.slow_callback_duration
    observer = UnknownCaAlert(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))

    async def observe() -> None:
        async with observer.capture():
            next_turn: asyncio.Future[None] = loop.create_future()
            loop.call_soon(next_turn.set_result, None)
            await next_turn

    loop.set_debug(False)
    loop.slow_callback_duration = 0.0
    try:
        await asyncio.create_task(observe())
        restored = (
            loop.get_exception_handler(),
            loop.get_debug(),
            loop.slow_callback_duration,
        )
    finally:
        loop.set_debug(original_debug)
        loop.slow_callback_duration = original_threshold

    assert restored == (original_handler, False, 0.0)
    assert all(record.name != "asyncio" for record in caplog.records)
