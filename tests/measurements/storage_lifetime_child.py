import asyncio
import gc
import inspect
import os
import sys
import time
import tracemalloc
from array import array
from pathlib import Path

from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.storage.files import RootManifest
from kora.plugins.storage.local import (
    COMPONENT,
    LocalArtifactStorage,
    LocalStorageConfiguration,
)
from kora.plugins.storage.native import filesystem
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import StorageLimits
from kora.protocols.time import SystemClock
from tests.measurements.harness import Report, new_report
from tests.measurements.storage_checkpoint import (
    append_checkpoint,
    start_checkpoint,
)
from tests.measurements.storage_lifetime_manifest import (
    RESOURCE_FIELDS,
    TIMING_FIELDS,
    WORKLOADS,
    samples,
    storage_report_json,
)
from tests.measurements.storage_lifetime_resources import (
    disk_counts,
    host_process_identity,
    process_ownership,
    retaining_paths,
    rss_bytes,
    sample_resources,
    source_digest,
)
from tests.measurements.storage_lifetime_workload import cycle
from tests.plugins.storage.pressure_native import command


async def measure(
    report: Report,
    output: Path,
    repeat: int,
    numbers: "array[float]",
    directory: Path,
    executed: bytearray,
    captured: bytearray,
    expected_revision: str,
    load_context: str,
    checkpoint_descriptor: int,
) -> None:
    root = Path(__file__).resolve().parents[2]
    checkout = command(("git", "rev-parse", "HEAD")).decode().strip()
    if checkout != expected_revision:
        raise AssertionError("the actual checkout does not match its declared revision")
    if load_context not in {
        "shared_local_sessions",
        "github_hosted_runner",
        "local_unspecified",
    }:
        raise AssertionError("the declared storage measurement load context is invalid")
    source = Path(inspect.getfile(LocalArtifactStorage)).resolve()
    if source != (root / "src/kora/plugins/storage/local.py").resolve():
        raise AssertionError("the installed provider is not this checkout")
    report.revision = checkout
    boot_session, process_identity = host_process_identity()
    report.environment.update(
        producer=repeat,
        pid=os.getpid(),
        boot_session=boot_session,
        process_identity=process_identity,
        source_digest=source_digest(root),
        installed_source=str(source.relative_to(root)),
        checkout_clean=not bool(command(("git", "status", "--porcelain"))),
        warmup_cycles=1,
        small_cycles=100,
        small_raw_includes_prefix=True,
        maximum_cycles=3,
        rss_method="proc_statm" if sys.platform == "linux" else "reaped_ps_kib",
        maxrss_meaning="cumulative_process_high_water",
        sampler_children_reaped=True,
        tracemalloc_nframe=1,
        numeric_capture="preallocated_array_d_report_population_after_tracing",
        timing="complete_cycle_including_verification_excluding_phase_sampling",
        unlinked_scope="all_process_regular_fds_on_root_device",
        load_context=load_context,
        checkpoint_format="fixed_rows_v1",
        checkpoint_descriptors=1,
    )
    parent_descriptor = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        report.environment["filesystem"] = filesystem(parent_descriptor)[0]
    finally:
        os.close(parent_descriptor)
    start_checkpoint(output, checkpoint_descriptor, report, repeat)
    descriptor = os.open(os.devnull, os.O_WRONLY)
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.CRITICAL),
        "storage-lifetime",
        clock.now,
        descriptor,
    )
    context = ProtocolContext(
        namespace_id="storage-measurement",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest="storage-lifetime",
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )
    ownership = process_ownership()
    width = len(RESOURCE_FIELDS) + len(TIMING_FIELDS) + 1

    def record(position: int, values: dict[str, int]) -> None:
        for name, value in values.items():
            numbers[position * width + 1 + RESOURCE_FIELDS.index(name)] = value

    tracemalloc.start(1)
    try:
        configuration = LocalStorageConfiguration(
            root=str(directory), limits=StorageLimits(version=1)
        )
        position = 0
        for workload in WORKLOADS:
            warmup_counts, _ = await cycle(
                configuration,
                workload,
                context,
                workload.name + "-warmup",
                lambda: None,
            )
            if process_ownership() != ownership:
                raise AssertionError(
                    "warmup changed pre-acquisition resource ownership"
                )
            baseline = position
            record(position, warmup_counts)
            del warmup_counts
            executed[position] = 1
            record(position, sample_resources(directory))
            captured[position] = 1
            position += 1
            manifest = RootManifest.model_validate_json(
                (directory / "manifest.json").read_bytes()
            )
            if report.environment["filesystem"] != manifest.filesystem:
                raise AssertionError("the actual root filesystem changed")
            del manifest
            append_checkpoint(
                checkpoint_descriptor, baseline, numbers, executed, captured
            )
            for measured in range(1, workload.cycles + 1):
                selected = position
                position += 1
                point = measured % 10 == 0 or workload.cycles == 3
                phase_rss = 0
                phase_bytes = 0
                phase_blocks = 0
                phase_seconds = 0.0

                def phase(sample_point: bool = point) -> None:
                    nonlocal phase_rss, phase_bytes, phase_blocks, phase_seconds
                    if sample_point:
                        phase_started = time.perf_counter()
                        phase_rss = max(phase_rss, rss_bytes())
                        disk = disk_counts(directory)
                        logical = disk["unique_bytes"]
                        allocated = disk["allocated_bytes"]
                        if not isinstance(logical, int) or not isinstance(
                            allocated, int
                        ):
                            raise AssertionError("physical counters must be integers")
                        phase_bytes = max(phase_bytes, logical)
                        phase_blocks = max(phase_blocks, allocated)
                        phase_seconds += time.perf_counter() - phase_started

                tracemalloc.reset_peak()
                started = time.perf_counter()
                counts, observation_seconds = await cycle(
                    configuration,
                    workload,
                    context,
                    workload.name + f"-{measured}",
                    phase,
                )
                gross = time.perf_counter() - started
                elapsed = gross - phase_seconds - observation_seconds
                numbers[selected * width] = elapsed
                timing_offset = selected * width + 1 + len(RESOURCE_FIELDS)
                for offset, duration in enumerate(
                    (gross, phase_seconds, observation_seconds)
                ):
                    numbers[timing_offset + offset] = duration
                executed[selected] = 1
                record(selected, counts)
                del counts
                if process_ownership() != ownership:
                    raise AssertionError(
                        "a cycle changed pre-acquisition resource ownership"
                    )
                if point:
                    record(
                        selected,
                        {
                            "phase_peak_rss_bytes": phase_rss,
                            "phase_peak_unique_bytes": phase_bytes,
                            "phase_peak_allocated_bytes": phase_blocks,
                        },
                    )
                    record(selected, sample_resources(directory))
                    captured[selected] = 1
                if measured in {50, 100}:
                    retaining_paths(output, f"{workload.name}-{measured}")
                if point:
                    for counter in ("threads", "descriptors", "tasks"):
                        if (
                            numbers[
                                selected * width + 1 + RESOURCE_FIELDS.index(counter)
                            ]
                            != numbers[
                                baseline * width + 1 + RESOURCE_FIELDS.index(counter)
                            ]
                        ):
                            raise AssertionError(f"the settled {counter} count grew")
                    for counter in (
                        "private_files",
                        "payload_links",
                        "unlinked_bytes",
                    ):
                        if (
                            numbers[
                                selected * width + 1 + RESOURCE_FIELDS.index(counter)
                            ]
                            != 0
                        ):
                            raise AssertionError(f"settled {counter} remain")
                append_checkpoint(
                    checkpoint_descriptor, selected, numbers, executed, captured
                )
            gc.collect()
            report.gates[workload.name] = True
        if position != len(report.samples):
            raise AssertionError(
                "the declared storage sample inventory was not executed"
            )
    finally:
        tracemalloc.stop()
        drained = diagnostics.shutdown()
        os.close(descriptor)
        if not drained or diagnostics.writer_is_alive():
            raise AssertionError("storage diagnostic shutdown did not close its writer")


def run(
    output: Path,
    repeat: int,
    directory: Path,
    expected_revision: str,
    load_context: str,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    report = new_report()
    report.samples = samples(repeat)
    report.gates = dict.fromkeys(
        (*[workload.name for workload in WORKLOADS], "complete"), False
    )
    width = len(RESOURCE_FIELDS) + len(TIMING_FIELDS) + 1
    numbers = array("d", [0.0]) * (len(report.samples) * width)
    executed = bytearray(len(report.samples))
    captured = bytearray(len(report.samples))
    started = time.perf_counter()
    try:
        descriptor = os.open(
            output / "rows.bin",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            asyncio.run(
                measure(
                    report,
                    output,
                    repeat,
                    numbers,
                    directory,
                    executed,
                    captured,
                    expected_revision,
                    load_context,
                    descriptor,
                )
            )
        finally:
            os.close(descriptor)
        report.gates["complete"] = True
    finally:
        report.durations["complete"] = time.perf_counter() - started
        for index, sample in enumerate(report.samples):
            sample.seconds = numbers[index * width]
            for field_index, name in enumerate(RESOURCE_FIELDS):
                sample.extra[name] = int(numbers[index * width + 1 + field_index])
            sample.extra["resource_sample"] = bool(captured[index])
            sample.extra["executed"] = bool(executed[index])
            for offset, name in enumerate(TIMING_FIELDS):
                sample.extra[name] = numbers[
                    index * width + 1 + len(RESOURCE_FIELDS) + offset
                ]
        (output / "report.json").write_text(
            storage_report_json(report), encoding="utf-8"
        )


if __name__ == "__main__":
    run(
        Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
    )
