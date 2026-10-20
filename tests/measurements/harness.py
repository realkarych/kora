import json
import os
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

OUTPUT_VARIABLE: str = "KORA_MEASUREMENT_OUTPUT"
REVISION_VARIABLE: str = "KORA_MEASUREMENT_REVISION"

ExtraValue = float | int | bool | str | None


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_extra() -> dict[str, ExtraValue]:
    return {}


@dataclass
class Sample:
    workload: str
    repeat: int
    count: int
    seconds: float
    extra: dict[str, ExtraValue] = field(default_factory=new_extra)

    def microseconds_per_record(self) -> float:
        return self.seconds / self.count * 1_000_000 if self.count else 0.0


def new_samples() -> list[Sample]:
    return []


def new_gates() -> dict[str, bool]:
    return {}


def new_durations() -> dict[str, float]:
    return {}


def new_environment() -> dict[str, ExtraValue]:
    return {}


@dataclass
class Report:
    started_at: str
    platform: str
    python: str
    revision: str
    samples: list[Sample] = field(default_factory=new_samples)
    gates: dict[str, bool] = field(default_factory=new_gates)
    durations: dict[str, float] = field(default_factory=new_durations)
    environment: dict[str, ExtraValue] = field(default_factory=new_environment)

    def summary(self) -> dict[str, dict[str, float]]:
        grouped: dict[str, list[float]] = {}
        for sample in self.samples:
            grouped.setdefault(sample.workload, []).append(
                sample.microseconds_per_record()
            )
        return {
            workload: {
                "median_us_per_record": statistics.median(values),
                "p95_us_per_record": sorted(values)[
                    max(0, int(len(values) * 0.95) - 1)
                ],
                "repeats": len(values),
            }
            for workload, values in grouped.items()
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at,
                "platform": self.platform,
                "python": self.python,
                "revision": self.revision,
                "environment": self.environment,
                "summary": self.summary(),
                "gates": self.gates,
                "durations_seconds": self.durations,
                "samples": [
                    {
                        "workload": s.workload,
                        "repeat": s.repeat,
                        "count": s.count,
                        "seconds": s.seconds,
                        "us_per_record": s.microseconds_per_record(),
                        **s.extra,
                    }
                    for s in self.samples
                ],
            },
            indent=2,
        )


def timed(action: Callable[[], None]) -> float:
    started = time.perf_counter()
    action()
    return time.perf_counter() - started


def open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def settled_counts(deadline_seconds: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + deadline_seconds
    previous = (threading.active_count(), open_descriptors())
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = (threading.active_count(), open_descriptors())
        if current == previous:
            return current
        previous = current
    return previous


def current_rss_bytes() -> int | None:
    statm = Path("/proc/self/statm")
    if not statm.exists():
        return None
    resident_pages = int(statm.read_text().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def new_report() -> Report:
    return Report(
        utc_now().isoformat(),
        sys.platform,
        sys.version.split()[0],
        os.environ.get(REVISION_VARIABLE, ""),
    )


def write_report(report: Report) -> None:
    output = os.environ.get(OUTPUT_VARIABLE)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.to_json(), encoding="utf-8")


def report_lifecycle() -> Iterator[Report]:
    created = new_report()
    yield created
    write_report(created)
