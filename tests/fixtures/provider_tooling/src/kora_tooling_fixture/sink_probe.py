import asyncio
import json
import os
import sys

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.observability import Level, LoggingSettings, configure_process
from kora.protocols.time import SystemClock


def fill(descriptor: int) -> None:
    os.set_blocking(descriptor, False)
    while True:
        try:
            os.write(descriptor, b"x" * 4096)
        except BlockingIOError:
            return


async def exercise(kind: str) -> dict[str, int | bool]:
    reading, writing = os.pipe()
    if kind == "broken":
        os.close(reading)
    else:
        fill(writing)
    diagnostics = configure_process(
        CLI_COMPONENT,
        LoggingSettings(component_levels={"protocols.loading": Level.DEBUG}),
        "listing-sink-probe",
        SystemClock().now,
        writing,
    )
    invocation = InstalledPluginDiscovery(diagnostics)
    try:
        result = await invocation.discover()
        before_shutdown = diagnostics.snapshot()
        drained = diagnostics.shutdown()
        snapshot = diagnostics.snapshot()
        return {
            "providers": len(result.providers),
            "failures": len(result.failures),
            "released": invocation.process is None,
            "dropped": snapshot.dropped_total(),
            "sink_failures": snapshot.sink_failures,
            "rejected": snapshot.rejected_total(),
            "queue_records": snapshot.queue_records,
            "queue_bytes": snapshot.queue_bytes,
            "in_flight_bytes": snapshot.in_flight_bytes,
            "queued_before_shutdown": before_shutdown.queue_records,
            "bytes_before_shutdown": before_shutdown.queue_bytes,
            "queue_capacity_loss": sum(
                amount
                for key, amount in snapshot.dropped.items()
                if key[2] == "queue_records"
            ),
            "drained": drained,
        }
    finally:
        diagnostics.shutdown()
        os.close(writing)
        if kind != "broken":
            os.close(reading)


if __name__ == "__main__":
    os.write(1, json.dumps(asyncio.run(exercise(sys.argv[1])), sort_keys=True).encode())
