import hashlib
import json
from pathlib import Path

import pytest

from tests.measurements.harness import Report, Sample, new_report
from tests.measurements.storage_budgets import read_budgets
from tests.measurements.storage_evidence_common import (
    EvidenceFailure,
    validate_execution,
)
from tests.measurements.storage_qualification_evidence import (
    InventoryPage,
    QualificationReport,
    QualificationSample,
    validate_boundaries,
)

REVISION: str = "a" * 40
DIGEST: str = "b" * 64


@pytest.mark.parametrize(
    "phase",
    ["page", "first", "middle", "final", "restart", "concurrent", "concurrent_page"],
)
def test_inventory_work_budget_covers_every_online_page_form(phase: str) -> None:
    report = qualification_report()
    changed: list[QualificationSample] = []
    for sample in report.samples:
        if (
            isinstance(sample, InventoryPage)
            and sample.workload == "storage.inventory." + phase
        ):
            sample = sample.model_copy(update={"query_steps": 100000})
        changed.append(sample)
    report = report.model_copy(update={"samples": tuple(changed)})

    with pytest.raises(EvidenceFailure, match="inventory\\."):
        validate_boundaries(report, REVISION, DIGEST)


@pytest.mark.parametrize(
    "phase",
    ["page", "first", "middle", "final", "restart", "concurrent", "concurrent_page"],
)
def test_inventory_timing_outlier_is_reported_for_every_online_page_form(
    phase: str,
) -> None:
    report = qualification_report()
    changed = tuple(
        sample.model_copy(update={"seconds": 10.0, "us_per_record": 10000000.0})
        if isinstance(sample, InventoryPage)
        and sample.workload == "storage.inventory." + phase
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})

    diagnostics = validate_boundaries(report, REVISION, DIGEST)

    assert {
        "metric": "storage.inventory." + phase + ".seconds",
        "observed": 10.0,
        "reference": read_budgets().platforms["linux"].inventory_page_seconds,
        "exceeds_reference": True,
    } in diagnostics


def test_inventory_budget_excludes_offline_seeding() -> None:
    report = qualification_report()
    changed = tuple(
        sample.model_copy(
            update={"seconds": 100.0, "us_per_record": 100000000.0 / sample.count}
        )
        if sample.workload == "storage.inventory.seed"
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})

    validate_boundaries(report, REVISION, DIGEST)

    assert report.samples[0].seconds == 100.0


def add_inventory_page(
    report: Report, population: int, phase: str, keys: list[str], cursor: str | None
) -> str | None:
    remaining = [key for key in keys if cursor is None or key > cursor]
    selected = remaining[:128]
    continuation = selected[-1] if len(selected) == 128 else None
    report.samples.append(
        Sample(
            f"storage.inventory.{phase}",
            population,
            1,
            0.1,
            {
                "query_steps": 800,
                "queried_rows": len(selected),
                "entries": len(selected),
                "metadata_bytes": 16000,
                "keys_json": json.dumps(selected),
                "cursor_key": cursor,
                "next_key": continuation,
            },
        )
    )
    return continuation


def qualification_report() -> QualificationReport:
    report = new_report()
    report.platform = "linux"
    report.revision = REVISION
    report.environment.update(
        source_digest=DIGEST,
        checkout_clean=True,
        core_authority="provider_harness_counters_not_durable_core_queries",
        installed_source="src/kora/plugins/storage/local.py",
        load_context="github_hosted_runner",
    )
    report.durations["complete"] = 5.0
    for population in (32, 256, 2048):
        report.samples.append(
            Sample("storage.inventory.seed", population, population, 0.1)
        )
        keys = sorted(
            "sha256:" + hashlib.sha256(index.to_bytes(16, "big")).hexdigest()
            for index in range(population)
        )
        cursor = None
        while True:
            cursor = add_inventory_page(report, population, "page", keys, cursor)
            if cursor is None:
                break
        for phase, cursor in (
            ("first", None),
            ("middle", keys[population // 2 - 1]),
            ("final", keys[-2]),
            ("restart", keys[-2]),
        ):
            add_inventory_page(report, population, phase, keys, cursor)
        changed = sorted(
            [*keys[1:], "sha256:" + hashlib.sha256(b"concurrent-key").hexdigest()]
        )
        cursor = add_inventory_page(report, population, "concurrent", changed, None)
        while cursor is not None:
            cursor = add_inventory_page(
                report, population, "concurrent_page", changed, cursor
            )
        report.gates[f"storage.inventory.{population}"] = True
    report.samples.append(
        Sample(
            "storage.receiver_churn",
            0,
            100,
            0.1,
            {
                "parts": 16,
                "refusals": 100,
                "reconstructions": 17,
                "authority_checks": 17,
                "continuing_useful_calls": 18,
            },
        )
    )
    report.samples.append(
        Sample(
            "storage.maximum_slots",
            0,
            1,
            0.1,
            {
                "stages": 32,
                "deletions": 32,
                "data_jobs": 8,
                "control_jobs": 2,
                "readers": 16,
                "composition_owners": 2,
                "peak_owned_jobs": 10,
            },
        )
    )
    report.samples.append(
        Sample(
            "storage.bounded_reconstruction",
            0,
            1,
            0.1,
            {
                "request_refusals": 3,
                "request_reconstructions": 3,
                "request_authority_checks": 3,
                "next_request_reconstructions": 1,
                "next_request_authority_checks": 1,
                "next_request_completed": True,
            },
        )
    )
    for name in ("receiver_churn", "maximum_slots", "bounded_reconstruction"):
        report.gates[f"storage.{name}"] = True
    for size in (0, 1, 65536, 16777215):
        for cycle in range(1, (3 if size == 16777215 else 1) + 1):
            report.samples.append(
                Sample(
                    "storage.prefix",
                    size,
                    1,
                    0.1,
                    {
                        "cycle": cycle,
                        "stage_rows": 0,
                        "delete_rows": 0,
                        "receipts": 0,
                        "content_rows": 0,
                        "reserved_bytes": 0,
                        "owned_jobs": 0,
                        "native_jobs": 0,
                        "readers": 0,
                        "connections": 0,
                    },
                )
            )
        report.gates[f"storage.prefix.{size}"] = True
    add_sinks(report)
    return QualificationReport.model_validate_json(report.to_json())


def add_sinks(report: Report) -> None:
    for sink in ("blocked", "broken"):
        for level in ("INFO", "DEBUG", "CRITICAL"):
            name = f"storage.sink.{sink}.{level}"
            active = level != "CRITICAL"
            report.samples.append(
                Sample(
                    name,
                    0,
                    150,
                    0.1,
                    {
                        "dropped": 5 if active else 0,
                        "sink_failed": active and sink == "broken",
                        "queue_records": 0,
                        "queue_bytes": 0,
                        "drained": not (active and sink == "blocked"),
                        "writer_alive_at_exit": active and sink == "blocked",
                        "filled_pipe_bytes": 65536 if sink == "blocked" else 0,
                        "provider_closed": True,
                        "payload_verified": True,
                        "actual_exit_status": 0,
                    },
                )
            )
            report.gates[name] = True


def test_qualification_parser_accepts_complete_boundary_evidence() -> None:
    report = qualification_report()

    validate_boundaries(report, REVISION, DIGEST)

    assert report.platform == "linux"


@pytest.mark.parametrize(
    "defect",
    [
        "source",
        "revision",
        "dirty",
        "missing_gate",
        "failed_gate",
        "missing_sample",
        "duplicate",
        "retained",
        "query",
        "population",
        "refusals",
        "missing_concurrent",
        "duplicate_page",
        "premature_probe_exhaustion",
    ],
)
def test_qualification_parser_rejects_false_boundary_success(defect: str) -> None:
    report = qualification_report()
    if defect == "source":
        changed = report.model_copy(
            update={
                "environment": report.environment.model_copy(
                    update={"source_digest": "c" * 64}
                )
            }
        )
    elif defect == "revision":
        changed = report.model_copy(update={"revision": "c" * 40})
    elif defect == "dirty":
        changed = report.model_copy(
            update={
                "environment": report.environment.model_copy(
                    update={"checkout_clean": False}
                )
            }
        )
    elif defect == "premature_probe_exhaustion":
        changed = report.model_copy(
            update={
                "samples": tuple(
                    sample.model_copy(update={"next_key": None})
                    if sample.workload == "storage.inventory.first"
                    and sample.repeat == 256
                    else sample
                    for sample in report.samples
                )
            }
        )
    elif defect == "missing_gate":
        changed = report.model_copy(
            update={
                "gates": {
                    name: value
                    for name, value in report.gates.items()
                    if name != "storage.maximum_slots"
                }
            }
        )
    elif defect == "failed_gate":
        changed = report.model_copy(
            update={"gates": {**report.gates, "storage.maximum_slots": False}}
        )
    elif defect == "missing_sample":
        changed = report.model_copy(update={"samples": report.samples[1:]})
    elif defect == "duplicate":
        changed = report.model_copy(
            update={"samples": (*report.samples, report.samples[0])}
        )
    elif defect == "missing_concurrent":
        changed = report.model_copy(
            update={
                "samples": tuple(
                    sample
                    for sample in report.samples
                    if sample.workload != "storage.inventory.concurrent_page"
                )
            }
        )
    elif defect == "duplicate_page":
        pages = [
            index
            for index, sample in enumerate(report.samples)
            if sample.workload == "storage.inventory.page" and sample.repeat == 256
        ]
        items = list(report.samples)
        items[pages[1]] = items[pages[0]]
        changed = report.model_copy(update={"samples": tuple(items)})
    else:
        target = {
            "retained": "storage.prefix",
            "query": "storage.inventory.page",
            "population": "storage.inventory.page",
            "refusals": "storage.receiver_churn",
        }[defect]
        values = {
            "retained": {"readers": 1},
            "query": {"query_steps": 0},
            "population": {"repeat": 64},
            "refusals": {"refusals": 99},
        }[defect]
        index = next(
            index
            for index, sample in enumerate(report.samples)
            if sample.workload == target
        )
        selected = report.samples[index].model_copy(update=values)
        changed = report.model_copy(
            update={
                "samples": (
                    *report.samples[:index],
                    selected,
                    *report.samples[index + 1 :],
                )
            }
        )

    with pytest.raises(EvidenceFailure):
        validate_boundaries(changed, REVISION, DIGEST)


@pytest.mark.parametrize("defect", ["status", "timeout", "overflow", "pid"])
def test_child_execution_parser_requires_actual_success(
    tmp_path: Path, defect: str
) -> None:
    data: dict[str, object] = {
        "pid": 1234,
        "status": 0,
        "timed_out": False,
        "overflow": False,
    }
    field, value = {
        "status": ("status", 1),
        "timeout": ("timed_out", True),
        "overflow": ("overflow", True),
        "pid": ("pid", 4321),
    }[defect]
    data[field] = value
    path = tmp_path / "execution.json"
    path.write_text(json.dumps(data))

    with pytest.raises(EvidenceFailure):
        validate_execution(path, 1234)
