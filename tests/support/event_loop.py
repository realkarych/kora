import asyncio


async def turn() -> None:
    loop = asyncio.get_running_loop()
    barrier: asyncio.Future[None] = loop.create_future()
    loop.call_soon(barrier.set_result, None)
    await barrier
