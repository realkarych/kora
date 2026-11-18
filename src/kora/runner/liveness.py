import asyncio
from collections.abc import Callable
from typing import Final

from kora.protocols.deadlines import NANOSECONDS_PER_SECOND, LocalThresholds
from kora.protocols.harness import RunnerCause
from kora.protocols.time import Clock
from kora.runner.sink import RunnerSink

WRITER_STALL_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND


async def watch_liveness(
    sink: RunnerSink,
    clock: Clock,
    stall_after_ns: int,
    deadline: LocalThresholds | None,
    ended: Callable[[], bool],
) -> None:
    while sink.cause() is None:
        now = clock.monotonic_ns()
        snapshot = sink.liveness()
        candidates: list[tuple[int, RunnerCause]] = []
        if snapshot.full_since_ns is not None:
            candidates.append(
                (snapshot.full_since_ns + WRITER_STALL_NS, "journal_unavailable")
            )
        if deadline is not None:
            candidates.append((deadline.interrupt_at_ns, "deadline"))
        if not snapshot.calls_open:
            candidates.append((snapshot.last_event_ns + stall_after_ns, "stall"))
        reached: list[RunnerCause] = [cause for at, cause in candidates if at <= now]
        if reached:
            if not ended():
                sink.terminate(reached[0])
            return
        due = [at for at, _ in candidates]
        changed = asyncio.create_task(sink.changed())
        stopped = asyncio.create_task(sink.termination())
        timer = asyncio.create_task(clock.sleep_until(min(due))) if due else None
        tasks = {changed, stopped} if timer is None else {changed, stopped, timer}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            for task in tasks:
                if not task.cancelled():
                    task.result()
