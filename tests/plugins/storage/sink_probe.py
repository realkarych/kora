import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import StrictBool, StrictInt

from kora.conformance.storage_support import (
    acknowledge_stage,
    operation,
    seal_repeated,
    stage_spec,
)
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.storage.local import COMPONENT, LocalStorageConfiguration
from kora.protocols.activity import NoActivity
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits
from kora.protocols.time import SystemClock
from tests.measurements.storage_lifetime_resources import close_provider
from tests.plugins.storage.inspection import InspectedStorage


class SinkReport(BoundaryModel):
    version: Literal[1] = 1
    sink: Literal["blocked", "broken"]
    level: Literal["INFO", "DEBUG", "CRITICAL"]
    completed_refusals: StrictInt
    payload_verified: StrictBool
    provider_closed: StrictBool
    dropped: StrictInt
    sink_failed: StrictBool
    queue_records: StrictInt
    queue_bytes: StrictInt
    drained: StrictBool
    writer_alive_at_exit: StrictBool
    filled_pipe_bytes: StrictInt
    elapsed_seconds: float


def fill(descriptor: int) -> int:
    os.set_blocking(descriptor, False)
    written = 0
    try:
        while True:
            written += os.write(descriptor, b"x" * 4096)
    except BlockingIOError:
        return written
    finally:
        os.set_blocking(descriptor, True)


def run(root: Path, sink: str, level: str) -> SinkReport:
    if sink not in {"blocked", "broken"} or level not in {"INFO", "DEBUG", "CRITICAL"}:
        raise ValueError("the storage sink fixture selection is invalid")
    read_end, write_end = os.pipe()
    filled = 0
    if sink == "blocked":
        filled = fill(write_end)
    else:
        os.close(read_end)
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level[level], queue_records=16, queue_bytes=65536),
        "storage-sink",
        clock.now,
        write_end,
    )
    context = ProtocolContext(
        namespace_id="storage-sink",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest="storage-sink",
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )
    provider = InspectedStorage(
        LocalStorageConfiguration(root=str(root), limits=StorageLimits(version=1))
    )
    completed = 0
    verified = False
    started = time.perf_counter()

    async def scenario() -> None:
        nonlocal completed, verified
        try:
            upload = await seal_repeated(provider, "live", 1, context)
            invalid = stage_spec(provider.identity(), "never-admitted", b"")
            request = operation(
                provider.identity(), "invalid", "begin_stage", invalid
            ).model_copy(update={"request_digest": "sha256:" + "0" * 64})
            for _ in range(150):
                try:
                    await provider.begin_stage(request, invalid, context)
                except KoraProtocolError as error:
                    if error.code != "storage_conflict":
                        raise
                    completed += 1
                else:
                    raise AssertionError("the real conflicting operation was accepted")
            reader = await provider.read_stage(upload.content, context)
            async with reader:
                value = await reader.read(1)
            verified = value == b"k"
            release = await acknowledge_stage(
                provider, upload.content.stage, upload.operations, context
            )
            await provider.release_stage(release, context)
        finally:
            await close_provider(provider)

    asyncio.run(scenario())
    before = diagnostics.snapshot()
    drained = diagnostics.shutdown(0.01 if sink == "blocked" else 1.0)
    after = diagnostics.snapshot()
    if drained:
        os.close(write_end)
        if sink == "blocked":
            os.close(read_end)
    return SinkReport.model_validate(
        {
            "version": 1,
            "sink": sink,
            "level": level,
            "completed_refusals": completed,
            "payload_verified": verified,
            "provider_closed": True,
            "dropped": before.dropped_total(),
            "sink_failed": after.sink_failed,
            "queue_records": after.queue_records,
            "queue_bytes": after.queue_bytes,
            "drained": drained,
            "writer_alive_at_exit": diagnostics.writer_is_alive(),
            "filled_pipe_bytes": filled,
            "elapsed_seconds": time.perf_counter() - started,
        }
    )


if __name__ == "__main__":
    result = run(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    sys.stdout.write(result.model_dump_json() + "\n")
