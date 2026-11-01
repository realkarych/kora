import asyncio
import sys
from pathlib import Path

from kora.conformance.capture import captured
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.serving import COMPONENT
from kora.plugins.runtime.local.supervisor import RunnerArguments, supervise
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.registry import Registry, Running
from tests.support.local_peers import CONDITION_SECONDS, asked_status, client, until
from tests.support.local_runtime import open_descriptors


async def cancel_supervisor(root: Path, domain: str, nonce: str) -> None:
    clock = SystemClock()
    directory = DirectoryAnchor.open_path(root / "registry")
    registry = Registry(directory, domain, lambda: "0" * 32)
    key = registry.key("attempt-1")
    try:
        with captured(
            COMPONENT, LoggingSettings(), "supervisor-cancellation", clock.now
        ) as capture:
            descriptors = open_descriptors()
            tasks = set(asyncio.all_tasks())
            supervising = asyncio.create_task(
                supervise(
                    RunnerArguments(root, "attempt-1", domain, nonce),
                    capture.diagnostics.logger(COMPONENT),
                )
            )
            try:
                await until(lambda: isinstance(registry.entry(key), Running))
                entry = registry.entry(key)
                assert isinstance(entry, Running)
                held = await client(entry.socket_path, clock)
                observing = await client(entry.socket_path, clock)
                try:
                    value = await asked_status(observing, clock)
                    assert value.status.state == "starting"

                    supervising.cancel()
                    asyncio.get_running_loop().call_soon(supervising.cancel)
                    result = await asyncio.gather(supervising, return_exceptions=True)

                    assert isinstance(result[0], asyncio.CancelledError)
                    await held.expect_eof(clock.monotonic_ns() + 5_000_000_000)
                finally:
                    held.close()
                    observing.close()
                assert open_descriptors() == descriptors - 1
                assert set(asyncio.all_tasks()) == tasks
                assert registry.entry(key) == entry
            finally:
                if not supervising.done():
                    supervising.cancel()
                    await asyncio.gather(supervising, return_exceptions=True)
    finally:
        directory.close()


def main() -> None:
    root, domain, nonce = sys.argv[1:]

    async def run() -> None:
        await asyncio.wait_for(
            cancel_supervisor(Path(root), domain, nonce), CONDITION_SECONDS
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
