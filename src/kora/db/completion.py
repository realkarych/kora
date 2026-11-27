import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

Result = TypeVar("Result")


async def wait_through_cancellations(task: asyncio.Future[Result]) -> None:
    while not task.done():
        try:
            await asyncio.shield(asyncio.wait({task}))
        except asyncio.CancelledError:
            continue
    if not task.cancelled():
        task.exception()


async def finish_releasing(
    pending: Awaitable[Result], release: Callable[[Result], Awaitable[None]]
) -> Result:
    task = asyncio.ensure_future(pending)
    return await wait_releasing(task, asyncio.shield(task), release)


async def wait_releasing(
    task: asyncio.Future[Result],
    protected: asyncio.Future[Result],
    release: Callable[[Result], Awaitable[None]],
) -> Result:
    try:
        return await protected
    except asyncio.CancelledError:
        await wait_through_cancellations(task)
        if not task.cancelled() and task.exception() is None:
            release_task = asyncio.ensure_future(release(task.result()))
            await wait_through_cancellations(release_task)
        raise


async def nothing(result: object) -> None:
    return None


async def complete(pending: Awaitable[Result]) -> Result:
    return await finish_releasing(pending, nothing)
