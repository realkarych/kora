import json
from dataclasses import dataclass, replace
from typing import Final

from kora.ir.json import parse_json_object
from tests.measurements.harness import Report, Sample


@dataclass(frozen=True)
class Workload:
    ending: str
    size: int
    cycles: int

    @property
    def name(self) -> str:
        return f"storage.{self.ending}.{self.size}"


SMALL_ENDINGS: Final[tuple[str, ...]] = (
    "raw",
    "prepared",
    "alias",
    "replay_conflict",
    "source_error",
    "cancelled",
    "lost_response",
    "expired",
)
SMALL_SIZES: Final[dict[str, int]] = {
    "raw": 65536,
    "prepared": 65536,
    "alias": 65536,
    "replay_conflict": 65536,
    "source_error": 65536,
    "cancelled": 65536,
    "lost_response": 0,
    "expired": 1,
}
WORKLOADS: Final[tuple[Workload, ...]] = tuple(
    Workload(ending, SMALL_SIZES[ending], 100) for ending in SMALL_ENDINGS
) + tuple(
    Workload(ending, size, 3)
    for ending in ("raw", "prepared", "alias")
    for size in (16777216, 268435456)
)
TIMING_FIELDS: Final[tuple[str, ...]] = (
    "gross_seconds",
    "phase_observer_seconds",
    "control_observer_seconds",
)
PRODUCERS: Final[frozenset[tuple[str, int]]] = frozenset(
    (platform, repeat) for platform in ("linux", "darwin") for repeat in range(3)
)
RESOURCE_FIELDS: Final[tuple[str, ...]] = (
    "phase_peak_rss_bytes",
    "phase_peak_unique_bytes",
    "phase_peak_allocated_bytes",
    "python_current_bytes",
    "python_post_gc_bytes",
    "python_peak_bytes",
    "tracer_bytes",
    "rss_bytes",
    "cumulative_maxrss_bytes",
    "threads",
    "descriptors",
    "tasks",
    "owned_jobs",
    "native_jobs",
    "readers",
    "connections",
    "stage_rows",
    "delete_rows",
    "receipts",
    "content_rows",
    "reserved_bytes",
    "database_bytes",
    "journal_bytes",
    "unique_bytes",
    "allocated_bytes",
    "private_files",
    "payload_links",
    "unlinked_bytes",
)


def samples(repeat: int) -> list[Sample]:
    return [
        Sample(
            workload.name,
            repeat,
            1,
            0.0,
            {
                "cycle": cycle,
                "phase": phase,
                "resource_sample": False,
                "executed": False,
                **dict.fromkeys(TIMING_FIELDS, 0.0),
                **dict.fromkeys(RESOURCE_FIELDS, 0),
            },
        )
        for workload in WORKLOADS
        for cycle, phase in (
            (0, "baseline"),
            *((cycle, "cycle") for cycle in range(1, workload.cycles + 1)),
        )
    ]


def storage_report_json(report: Report) -> str:
    document = parse_json_object(json.loads(report.to_json()))
    timed = replace(
        report,
        samples=[
            sample
            for sample in report.samples
            if sample.extra.get("phase") == "cycle"
            and sample.extra.get("executed") is True
        ],
    )
    document["summary"] = parse_json_object(timed.summary())
    return json.dumps(document, indent=2)
