import asyncio
import errno
import os
import sqlite3
import sys
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from pydantic import StrictStr

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.storage_support import (
    RepeatedSource,
    operation,
    repeated_digest,
    seal_repeated,
)
from kora.observability import LoggingSettings
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    PartSpec,
    StageRef,
    StageSpec,
    StageWrite,
    StorageLimits,
)
from tests.plugins.storage.pressure_models import PressureFailure, PressureReport


class PressureStart(BoundaryModel):
    mount: StrictStr
    workload: Literal["enospc", "child-failure", "hold"]


def fill(descriptor: int, size: int) -> None:
    chunk = b"f" * 65536
    for offset in range(0, size, len(chunk)):
        remaining = memoryview(chunk)[: min(len(chunk), size - offset)]
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise PressureFailure("filler_write_failed")
            remaining = remaining[written:]
    os.fsync(descriptor)


def native_failure_codes(error: BaseException) -> tuple[int | None, int | None]:
    current: BaseException | None = error
    native: int | None = None
    database: int | None = None
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, OSError):
            native = current.errno
        if isinstance(current, sqlite3.Error) and hasattr(current, "sqlite_errorcode"):
            database = current.sqlite_errorcode & 255
        current = (
            current.__cause__ if current.__cause__ is not None else current.__context__
        )
    return native, database


async def pressure(start: PressureStart, connection: Connection) -> PressureReport:
    mount = Path(start.mount)
    provider = LocalArtifactStorage(
        LocalStorageConfiguration(
            root=str(mount / "store"), limits=StorageLimits(version=1)
        )
    )
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "storage-pressure", utc_now
    ) as capture:
        context = context_for(capture)
        filler = mount / "filler"
        try:
            if start.workload == "hold":
                await seal_repeated(provider, "held-stage", 65536, context)
            connection.send_bytes(b"provider_started")
            if start.workload == "hold":
                try:
                    await asyncio.to_thread(connection.recv_bytes, 64)
                except EOFError:
                    return PressureReport(
                        provider_started=True, failure="controller_control_closed"
                    )
                return PressureReport(
                    provider_started=True, failure="controller_cancelled"
                )
            if start.workload == "child-failure":
                return PressureReport(provider_started=True, failure="child_failure")
            descriptor = os.open(
                filler, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                fill(descriptor, 33554432)
                stage = StageRef(
                    version=1,
                    storage_identity=provider.identity(),
                    stage_id="pressure-stage",
                )
                spec = StageSpec(
                    version=1,
                    stage=stage,
                    payload_digest=repeated_digest(268435456),
                    total_size=268435456,
                    content_type="application/octet-stream",
                )
                await provider.begin_stage(
                    operation(
                        provider.identity(), "pressure-begin", "begin_stage", spec
                    ),
                    spec,
                    context,
                )
                failed = None
                payload_errno = None
                for index in range(16):
                    part = PartSpec(
                        version=1,
                        index=index,
                        byte_offset=index * 16777216,
                        size=16777216,
                        digest=repeated_digest(16777216),
                        final=index == 15,
                    )
                    write = operation(
                        provider.identity(),
                        f"pressure-part-{index}",
                        "write_part",
                        StageWrite(version=1, stage=stage, spec=part),
                    )
                    try:
                        await provider.write_part(
                            write, stage, part, RepeatedSource(part.size), context
                        )
                    except KoraProtocolError as error:
                        if error.code != "storage_io":
                            raise
                        payload_errno, _ = native_failure_codes(error)
                        if payload_errno != errno.ENOSPC:
                            raise PressureFailure("payload_enospc_not_proven") from None
                        failed = write
                        break
                if failed is None:
                    raise PressureFailure("payload_enospc_not_observed")
                observed = await provider.observe(failed, context)
                inventory = await provider.scan("stages", None, 128, context)
                charged = (
                    observed.state == "pending"
                    and len(inventory.entries) == 1
                    and inventory.entries[0].size > 0
                )
                for count in (65536, 4096, 512, 1):
                    for _ in range(67108864 // count + 1):
                        try:
                            fill(descriptor, count)
                        except OSError as error:
                            if error.errno != errno.ENOSPC:
                                raise
                            break
                    else:
                        raise PressureFailure("filler_bound_exceeded")
                metadata_spec = spec.model_copy(
                    update={
                        "stage": stage.model_copy(
                            update={"stage_id": "metadata-pressure"}
                        )
                    }
                )
                filesystem = os.statvfs(mount)
                headroom = filesystem.f_frsize
                size = os.fstat(descriptor).st_size
                if headroom <= 0 or size <= headroom:
                    raise PressureFailure("metadata_headroom_unavailable")
                os.ftruncate(descriptor, size - headroom)
                os.fsync(descriptor)
                metadata_failed = False
                metadata_errno = None
                metadata_sqlite_code = None
                try:
                    await provider.begin_stage(
                        operation(
                            provider.identity(),
                            "metadata-begin",
                            "begin_stage",
                            metadata_spec,
                        ),
                        metadata_spec,
                        context,
                    )
                except KoraProtocolError as error:
                    if error.code != "storage_io":
                        raise
                    metadata_errno, metadata_sqlite_code = native_failure_codes(error)
                    if (
                        metadata_errno != errno.ENOSPC
                        and metadata_sqlite_code != sqlite3.SQLITE_FULL
                    ):
                        return PressureReport(
                            provider_started=True,
                            payload_enospc=True,
                            payload_errno=payload_errno,
                            metadata_errno=metadata_errno,
                            metadata_sqlite_code=metadata_sqlite_code,
                            metadata_headroom_bytes=headroom,
                            failure="metadata_enospc_not_proven",
                        )
                    metadata_failed = True
            finally:
                os.close(descriptor)
                filler.unlink()
                directory = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            retired = await provider.retire_stage(
                operation(
                    provider.identity(), "pressure-retire", "retire_stage", stage
                ),
                stage,
                context,
            )
            return PressureReport(
                provider_started=True,
                payload_enospc=True,
                metadata_enospc=metadata_failed,
                payload_errno=payload_errno,
                metadata_sqlite_code=metadata_sqlite_code,
                metadata_errno=metadata_errno,
                metadata_headroom_bytes=headroom,
                unknown_state_charged=charged,
                cleanup_completed=retired.completed,
            )
        finally:
            await provider.aclose()


def main(descriptor: int) -> int:
    connection = Connection(descriptor)
    try:
        connection.send_bytes(b"ready")
        if not connection.poll(90):
            raise PressureFailure("controller_start_deadline")
        try:
            encoded = connection.recv_bytes(8192)
        except EOFError:
            return 0
        start = PressureStart.model_validate_json(encoded)
        try:
            report = asyncio.run(pressure(start, connection))
        except PressureFailure as error:
            connection.send_bytes(
                PressureReport(failure=error.code).model_dump_json().encode()
            )
            return 1
        connection.send_bytes(report.model_dump_json().encode())
        return 0 if report.failure is None else 1
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise PressureFailure("child_arguments")
    raise SystemExit(main(int(sys.argv[1])))
