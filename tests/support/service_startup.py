import asyncio
import os
import sys
from pathlib import Path

from kora.conformance.capture import captured
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import SystemClock
from kora.runner.registry import COMPONENT
from tests.support.local_runtime import runtime_context


async def inspect(root: Path, result: Path) -> None:
    clock = SystemClock()
    with captured(
        COMPONENT, LoggingSettings(), "service-startup", clock.now
    ) as capture:
        try:
            runtime = await LocalRuntime.open(
                LocalRuntimeConfiguration(state_root=str(root)),
                runtime_context(capture),
            )
        except KoraProtocolError as error:
            result.write_text(str(error.details.get("reason", error.code)))
        else:
            await runtime.close()
            result.write_text("accepted")


def main() -> None:
    os.umask(0o077)
    root, result = map(Path, sys.argv[1:])
    asyncio.run(inspect(root, result))


if __name__ == "__main__":
    main()
