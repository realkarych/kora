import asyncio


class LoopBoundWorker:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[asyncio.Future[None] | None]
    worker: asyncio.Task[None]
    cleanup_loop: asyncio.AbstractEventLoop | None

    def start_loop_worker(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.cleanup_loop = None

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def pass_through(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await self.worker
        self.cleanup_loop = asyncio.get_running_loop()
