import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fixtures.provider_tooling.src.kora_tooling_fixture.reaper import (
        DeferredReaper,
    )
else:
    from kora_tooling_fixture.reaper import DeferredReaper

from kora.cli import CLI_COMPONENT
from kora.conformance.command import ConformanceCommand
from kora.conformance.io_helper import InstalledIOHelperLauncher, IOHelper
from kora.conformance.result import ChildInput, InputFiles
from kora.observability import LoggingSettings, configure_process
from kora.protocols.metadata import ProtocolName
from kora.protocols.time import Clock


def utc_now() -> datetime:
    return datetime.now(UTC)


class CountingLauncher:
    def __init__(self, inner: InstalledIOHelperLauncher) -> None:
        self.inner = inner
        self.starts = 0

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        self.starts += 1
        return self.inner.start(descriptors, clock)


async def run(home: Path) -> None:
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "command-reap", utc_now
    )
    reaper = DeferredReaper()
    launcher = CountingLauncher(
        InstalledIOHelperLauncher(home=home, state_home=home / "state", reaper=reaper)
    )
    command = ConformanceCommand(diagnostics, launcher=launcher)
    with TemporaryDirectory(prefix="kora-reap-", dir="/tmp") as temporary:
        folder = Path(temporary).resolve()
        config = folder / "config.json"
        data = folder / "data.json"
        config.write_text(json.dumps({"value": "private-reap-value"}))
        data.write_text(json.dumps({"value": "private-reap-exercise"}))
        request = ChildInput(
            protocol=ProtocolName.SECRETS,
            name="configured",
            fixtures="kora_tooling_fixture.fixtures_wrong_bundle",
            files=InputFiles(configuration=str(config), fixture_data=str(data)),
        )
        try:
            async with asyncio.timeout(20):
                result = await command.run(request)
        finally:
            diagnostics.shutdown()
    os.write(
        1,
        json.dumps(
            {
                "result": json.loads(result.model_dump_json()),
                "deferred": len(reaper.deferred),
                "starts": launcher.starts,
                "held": len(reaper.held),
                "retained_helpers": command.owner.inventory().helpers.retained_helpers,
            }
        ).encode(),
    )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1])))
