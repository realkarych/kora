import asyncio
import os
import signal
import sys
from pathlib import Path

from kora.conformance.capture import captured
from kora.executor.launcher import ProcessLauncher
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.protocols.time import SystemClock
from kora.runner.registry import COMPONENT, Running
from tests.support.local_runners import submitted
from tests.support.local_runtime import recorded, runtime_context
from tests.support.service_tools import LingeringLauncher


async def serve(root: Path, sockets: Path, ready: Path, linger_false: bool) -> None:
    clock = SystemClock()
    with captured(
        COMPONENT, LoggingSettings(), "service-fixture", clock.now
    ) as capture:
        ctx = runtime_context(capture)
        runtime = await LocalRuntime.open(
            LocalRuntimeConfiguration(state_root=str(root)),
            ctx,
            socket_directory=None if linger_false else sockets,
            service_launcher=LingeringLauncher(ProcessLauncher())
            if linger_false
            else None,
        )
        try:
            key = runtime.registry.key("service-attempt")
            entry = await runtime.entry(key)
            if entry is None:
                await runtime.workspace_target("service-attempt", ctx)
                await runtime.submit(submitted(runtime, "service-attempt"), ctx)
                entry = await recorded(runtime, "service-attempt")
            assert isinstance(entry, Running)
            status = await runtime.status(key.handle(), ctx)
            assert status.state == "starting"
            temporary = ready.with_suffix(".pending")
            temporary.write_text(entry.model_dump_json())
            temporary.replace(ready)
            await asyncio.Future[None]()
        finally:
            await runtime.close()


def main() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    os.umask(0o077)
    signal.alarm(120)
    root, sockets, ready = map(Path, sys.argv[1:4])
    linger_false = sys.argv[4:] == ["linger-false"]
    asyncio.run(serve(root, sockets, ready, linger_false))


if __name__ == "__main__":
    main()
