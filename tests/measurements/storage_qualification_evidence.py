import errno
import sqlite3
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter

from kora.ir.scalars import Digest
from kora.protocols.common import BoundaryModel
from tests.measurements.storage_evidence_common import (
    EvidenceFailure,
    junit_nodes,
    read_json,
    validate_execution,
)
from tests.plugins.storage.pressure_models import PressureManifest, PressureReport
from tests.plugins.storage.sink_probe import SinkReport

Nonnegative = Annotated[StrictInt, Field(ge=0)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PAGE_KEYS: Final[TypeAdapter[tuple[Digest, ...]]] = TypeAdapter(tuple[Digest, ...])


class QualificationEnvironment(BoundaryModel):
    source_digest: StrictStr
    checkout_clean: StrictBool
    core_authority: Literal["provider_harness_counters_not_durable_core_queries"]
    installed_source: Literal["src/kora/plugins/storage/local.py"]
    load_context: Literal[
        "shared_local_sessions", "github_hosted_runner", "local_unspecified"
    ]


class BoundarySample(BoundaryModel):
    repeat: Nonnegative
    count: Nonnegative
    seconds: Seconds
    us_per_record: Seconds


class InventorySeed(BoundarySample):
    workload: Literal["storage.inventory.seed"]


class InventoryPage(BoundarySample):
    workload: Literal[
        "storage.inventory.page",
        "storage.inventory.first",
        "storage.inventory.middle",
        "storage.inventory.final",
        "storage.inventory.restart",
        "storage.inventory.concurrent",
        "storage.inventory.concurrent_page",
    ]
    query_steps: Nonnegative
    queried_rows: Annotated[StrictInt, Field(ge=0, le=128)]
    entries: Annotated[StrictInt, Field(ge=0, le=128)]
    metadata_bytes: Annotated[StrictInt, Field(ge=1, le=16384)]
    keys_json: Annotated[StrictStr, Field(max_length=16384)]
    cursor_key: Digest | None
    next_key: Digest | None


class ReceiverChurn(BoundarySample):
    workload: Literal["storage.receiver_churn"]
    parts: Nonnegative
    refusals: Nonnegative
    reconstructions: Nonnegative
    authority_checks: Nonnegative
    continuing_useful_calls: Nonnegative


class MaximumSlots(BoundarySample):
    workload: Literal["storage.maximum_slots"]
    stages: Nonnegative
    deletions: Nonnegative
    data_jobs: Nonnegative
    control_jobs: Nonnegative
    readers: Nonnegative
    composition_owners: Nonnegative
    peak_owned_jobs: Nonnegative


class Reconstruction(BoundarySample):
    workload: Literal["storage.bounded_reconstruction"]
    request_refusals: Nonnegative
    request_reconstructions: Nonnegative
    request_authority_checks: Nonnegative
    next_request_reconstructions: Nonnegative
    next_request_authority_checks: Nonnegative
    next_request_completed: StrictBool


class PrefixSample(BoundarySample):
    workload: Literal["storage.prefix"]
    cycle: Nonnegative
    stage_rows: Nonnegative
    delete_rows: Nonnegative
    receipts: Nonnegative
    content_rows: Nonnegative
    reserved_bytes: Nonnegative
    owned_jobs: Nonnegative
    native_jobs: Nonnegative
    readers: Nonnegative
    connections: Nonnegative


class SinkSample(BoundarySample):
    workload: Literal[
        "storage.sink.blocked.INFO",
        "storage.sink.broken.INFO",
        "storage.sink.blocked.DEBUG",
        "storage.sink.broken.DEBUG",
        "storage.sink.blocked.CRITICAL",
        "storage.sink.broken.CRITICAL",
    ]
    dropped: Nonnegative
    sink_failed: StrictBool
    queue_records: Nonnegative
    queue_bytes: Nonnegative
    drained: StrictBool
    writer_alive_at_exit: StrictBool
    filled_pipe_bytes: Nonnegative
    provider_closed: StrictBool
    payload_verified: StrictBool
    actual_exit_status: StrictInt


QualificationSample = Annotated[
    InventorySeed
    | InventoryPage
    | ReceiverChurn
    | MaximumSlots
    | Reconstruction
    | PrefixSample
    | SinkSample,
    Field(discriminator="workload"),
]


class QualificationReport(BoundaryModel):
    started_at: StrictStr
    platform: Literal["linux", "darwin"]
    python: StrictStr
    revision: StrictStr
    environment: QualificationEnvironment
    summary: dict[str, dict[str, float]]
    gates: dict[str, StrictBool]
    durations_seconds: dict[str, Seconds]
    samples: tuple[QualificationSample, ...]


class PressureCleanup(BoundaryModel):
    manifests: Nonnegative
    failures: tuple[StrictStr, ...]


def qualification_nodes() -> frozenset[str]:
    boundary = "tests.plugins.storage.test_lifetime_boundaries."
    pressure = "tests.plugins.storage.test_pressure_controller."
    return frozenset(
        {
            *(
                boundary + f"test_local_storage_inventory_query_work[{count}]"
                for count in (32, 256, 2048)
            ),
            boundary + "test_local_storage_receiver_churn_is_bounded",
            boundary + "test_local_storage_control_progress_under_saturation",
            boundary + "test_three_epoch_changes_return_pending_"
            "before_a_later_request_progresses",
            *(
                boundary + f"test_local_storage_prefix_boundaries[{count}]"
                for count in (0, 1, 65536, 16777215)
            ),
            *(
                boundary
                + f"test_boundary_setup_cancellation_closes_actual_ownership[{kind}]"
                for kind in ("slots", "churn", "reconstruction", "reconstruction_exit")
            ),
            *(
                "tests.plugins.storage.test_sink_progress.test_storage_cleanup_and_exit_progress_with_unavailable_sink["
                + level
                + "-"
                + sink
                + "]"
                for level in ("INFO", "DEBUG", "CRITICAL")
                for sink in ("blocked", "broken")
            ),
            pressure + "test_closed_failed_command_does_not_block_pressure_recovery",
            *(
                pressure
                + "test_pressure_owner_closes_after_parent_interrupt["
                + ending
                + "-"
                + reason
                + "]"
                for ending, reason in (
                    ("parent-eof", "controller_control_closed"),
                    ("cancelled", "controller_cancelled"),
                    ("deadline", "child_deadline"),
                )
            ),
            pressure
            + "test_pressure_owner_refuses_writable_ancestor_before_acquisition",
            pressure + "test_pressure_owner_refuses_symlinked_acquisition_ancestor",
            pressure + "test_local_storage_enospc_recovers",
            pressure + "test_storage_pressure_controller_closes_on_child_failure",
            pressure + "test_storage_pressure_controller_recovers_partial_acquisition",
            pressure + "test_pressure_owner_reconciles_lost_attachment_response",
            pressure + "test_failed_unmount_retains_ownership_until_the_blocker_closes",
            pressure + "test_unknown_image_identity_refuses_cleanup_before_detach",
            pressure + "test_pressure_owner_recovers_interrupted_manifest_write",
            pressure + "test_pressure_recovery_lease_refuses_a_concurrent_owner",
            *(
                "tests.plugins.storage.test_crash_recovery.test_promotion_process_crash_preserves_original_attribution[storage.promote."
                + phase
                + "]"
                for phase in ("registered", "linked", "effect_persisted", "admitted")
            ),
        }
    )


def checked_page_keys(page: InventoryPage) -> tuple[str, ...]:
    keys = PAGE_KEYS.validate_json(page.keys_json)
    if (
        len(keys) != page.entries
        or keys != tuple(sorted(set(keys)))
        or (page.cursor_key is not None and keys and keys[0] <= page.cursor_key)
        or (page.next_key is not None and (not keys or page.next_key != keys[-1]))
    ):
        raise EvidenceFailure("inventory page keys or continuation are inconsistent")
    return keys


def validate_pages(pages: tuple[InventoryPage, ...], count: int) -> tuple[str, ...]:
    if not pages:
        raise EvidenceFailure("a required inventory page sequence is missing")
    cursor: str | None = None
    keys: list[str] = []
    for index, page in enumerate(pages):
        if page.cursor_key != cursor or (index and cursor is None):
            raise EvidenceFailure(
                "inventory pages are missing, duplicated or out of order"
            )
        keys.extend(checked_page_keys(page))
        cursor = page.next_key
    if cursor is not None or len(keys) != count or keys != sorted(set(keys)):
        raise EvidenceFailure("the complete unique inventory keyset was not paged")
    return tuple(keys)


def validate_boundaries(
    report: QualificationReport, revision: str, digest: str
) -> None:
    if (
        report.revision != revision
        or report.environment.source_digest != digest
        or not report.environment.checkout_clean
    ):
        raise EvidenceFailure(
            "qualification source/revision is not the exact clean checkout"
        )
    gates = {
        *(f"storage.inventory.{count}" for count in (32, 256, 2048)),
        *(f"storage.prefix.{count}" for count in (0, 1, 65536, 16777215)),
        "storage.receiver_churn",
        "storage.maximum_slots",
        "storage.bounded_reconstruction",
        *(
            f"storage.sink.{sink}.{level}"
            for sink in ("blocked", "broken")
            for level in ("INFO", "DEBUG", "CRITICAL")
        ),
    }
    if set(report.gates) != gates or not all(report.gates.values()):
        raise EvidenceFailure("qualification workload gates are incomplete")
    for count in (32, 256, 2048):
        selected = tuple(sample for sample in report.samples if sample.repeat == count)
        for phase in ("seed", "first", "middle", "final", "restart", "concurrent"):
            if (
                sum(
                    sample.workload == f"storage.inventory.{phase}"
                    for sample in selected
                )
                != 1
            ):
                raise EvidenceFailure(
                    "an inventory population/phase is missing or duplicated"
                )
        ordinary = tuple(
            sample
            for sample in selected
            if isinstance(sample, InventoryPage)
            and sample.workload == "storage.inventory.page"
        )
        concurrent = tuple(
            sample
            for sample in selected
            if isinstance(sample, InventoryPage)
            and sample.workload
            in {"storage.inventory.concurrent", "storage.inventory.concurrent_page"}
        )
        original_keys = validate_pages(ordinary, count)
        changed_keys = validate_pages(concurrent, count)
        if (
            len(set(original_keys) - set(changed_keys)) != 1
            or len(set(changed_keys) - set(original_keys)) != 1
        ):
            raise EvidenceFailure(
                "concurrent inventory did not create and delete content"
            )
        for phase, cursor in (
            ("first", None),
            ("middle", original_keys[count // 2 - 1]),
            ("final", original_keys[-2]),
            ("restart", original_keys[-2]),
        ):
            page = next(
                sample
                for sample in selected
                if isinstance(sample, InventoryPage)
                and sample.workload == f"storage.inventory.{phase}"
            )
            keys = checked_page_keys(page)
            remaining = tuple(
                key for key in original_keys if cursor is None or key > cursor
            )
            if (
                page.cursor_key != cursor
                or keys != remaining[: len(keys)]
                or not keys
                or (page.next_key is None) != (len(keys) == len(remaining))
            ):
                raise EvidenceFailure(
                    "an inventory probe differs from its keyset position"
                )
    prefixes = {
        (sample.repeat, sample.cycle)
        for sample in report.samples
        if isinstance(sample, PrefixSample)
    }
    if (
        prefixes
        != {(0, 1), (1, 1), (65536, 1), (16777215, 1), (16777215, 2), (16777215, 3)}
        or sum(isinstance(sample, PrefixSample) for sample in report.samples) != 6
    ):
        raise EvidenceFailure("the prefix boundary cycle union is incomplete")
    for kind in (ReceiverChurn, MaximumSlots, Reconstruction):
        if sum(isinstance(sample, kind) for sample in report.samples) != 1:
            raise EvidenceFailure("a lifecycle boundary is missing or duplicated")
    for sample in report.samples:
        if isinstance(sample, (InventorySeed, InventoryPage)) and sample.repeat not in {
            32,
            256,
            2048,
        }:
            raise EvidenceFailure("an undeclared inventory population was supplied")
        if isinstance(sample, InventorySeed) and sample.count != sample.repeat:
            raise EvidenceFailure("the inventory seed population is inconsistent")
        if (
            isinstance(
                sample, (InventoryPage, MaximumSlots, Reconstruction, PrefixSample)
            )
            and sample.count != 1
        ):
            raise EvidenceFailure(
                "a qualification count differs from its declared action"
            )
        if (
            isinstance(
                sample, (ReceiverChurn, MaximumSlots, Reconstruction, SinkSample)
            )
            and sample.repeat != 0
        ):
            raise EvidenceFailure(
                "a qualification repeat differs from its declared action"
            )
        if isinstance(sample, ReceiverChurn) and sample.count != 100:
            raise EvidenceFailure("the receiver-churn release count is incomplete")
        if (
            sample.seconds <= 0
            or sample.count <= 0
            or abs(sample.us_per_record - sample.seconds / sample.count * 1000000)
            > 1e-6
        ):
            raise EvidenceFailure("a qualification timing/count is invalid")
        if isinstance(sample, InventoryPage) and (
            sample.entries > sample.queried_rows or sample.query_steps == 0
        ):
            raise EvidenceFailure("actual inventory query work is absent")
        if isinstance(sample, ReceiverChurn) and (
            sample.parts,
            sample.refusals,
            sample.reconstructions,
            sample.authority_checks,
            sample.continuing_useful_calls,
        ) != (16, 100, 17, 17, 18):
            raise EvidenceFailure(
                "continuing-owner churn did not complete its declared work"
            )
        if isinstance(sample, MaximumSlots) and (
            sample.stages,
            sample.deletions,
            sample.data_jobs,
            sample.control_jobs,
            sample.readers,
            sample.composition_owners,
            sample.peak_owned_jobs,
        ) != (32, 32, 8, 2, 16, 2, 10):
            raise EvidenceFailure("maximum simultaneous ownership was not exercised")
        if isinstance(sample, Reconstruction) and (
            (
                sample.request_refusals,
                sample.request_reconstructions,
                sample.request_authority_checks,
                sample.next_request_reconstructions,
                sample.next_request_authority_checks,
            )
            != (3, 3, 3, 1, 1)
            or not sample.next_request_completed
        ):
            raise EvidenceFailure("bounded reconstruction/progress was not exercised")
        if isinstance(sample, PrefixSample) and any(
            (
                sample.stage_rows,
                sample.delete_rows,
                sample.receipts,
                sample.content_rows,
                sample.reserved_bytes,
                sample.owned_jobs,
                sample.native_jobs,
                sample.readers,
                sample.connections,
            )
        ):
            raise EvidenceFailure("prefix cleanup retained disposable ownership")
        if isinstance(sample, SinkSample) and (
            sample.count != 150
            or sample.queue_records
            or sample.queue_bytes
            or sample.actual_exit_status != 0
            or not sample.provider_closed
            or not sample.payload_verified
        ):
            raise EvidenceFailure(
                "unavailable diagnostics prevented provider closure/progress"
            )


def validate_qualification(directory: Path, revision: str, digest: str) -> str:
    report = QualificationReport.model_validate(
        read_json(directory / "boundaries.json")
    )
    validate_boundaries(report, revision, digest)
    if junit_nodes(directory / "qualification.xml") != qualification_nodes():
        raise EvidenceFailure("the exact qualification test inventory is incomplete")
    for sink in ("blocked", "broken"):
        for level in ("INFO", "DEBUG", "CRITICAL"):
            stem = directory / "sinks" / f"{sink}-{level}"
            validate_execution(stem.with_suffix(".execution.json"))
            if stem.with_suffix(".stderr").read_bytes():
                raise EvidenceFailure(
                    "a diagnostic-failure child emitted unexpected errors"
                )
            actual = SinkReport.model_validate(
                read_json(stem.with_suffix(".stdout"), 16384)
            )
            expected = tuple(
                sample
                for sample in report.samples
                if isinstance(sample, SinkSample)
                and sample.workload == f"storage.sink.{sink}.{level}"
            )
            if (
                len(expected) != 1
                or actual.sink != sink
                or actual.level != level
                or actual.completed_refusals != expected[0].count
                or actual.elapsed_seconds != expected[0].seconds
                or actual.dropped != expected[0].dropped
                or any(
                    getattr(actual, name) != getattr(expected[0], name)
                    for name in (
                        "payload_verified",
                        "provider_closed",
                        "sink_failed",
                        "queue_records",
                        "queue_bytes",
                        "drained",
                        "writer_alive_at_exit",
                        "filled_pipe_bytes",
                    )
                )
            ):
                raise EvidenceFailure(
                    "diagnostic-failure raw evidence does not match its workload"
                )
    cleanup = PressureCleanup.model_validate(
        read_json(directory / "pressure/cleanup.json", 16384)
    )
    if cleanup.manifests != 12 or cleanup.failures:
        raise EvidenceFailure("pressure fixture ownership/cleanup is incomplete")
    directories: set[str] = set()
    nonces: set[str] = set()
    for index in range(cleanup.manifests):
        before = PressureManifest.model_validate(
            read_json(directory / "pressure" / f"before-{index}.manifest.json", 16384)
        )
        after = PressureManifest.model_validate(
            read_json(directory / "pressure" / f"after-{index}.manifest.json", 16384)
        )
        if before.directory in directories or before.nonce in nonces:
            raise EvidenceFailure("pressure ownership evidence is duplicated")
        directories.add(before.directory)
        nonces.add(before.nonce)
        if (
            (
                before.directory,
                before.directory_stamp,
                before.lease_stamp,
                before.nonce,
                before.uid,
                before.platform,
            )
            != (
                after.directory,
                after.directory_stamp,
                after.lease_stamp,
                after.nonce,
                after.uid,
                after.platform,
            )
            or after.platform != report.platform
            or not after.resources_closed
            or not after.detached
            or after.command_inflight
        ):
            raise EvidenceFailure(
                "pressure cleanup changed ownership or left an unresolved effect"
            )
    primary = tuple(
        PressureReport.model_validate(read_json(path, 16384))
        for path in (directory / "pressure").glob("primary-*.json")
    )
    final = tuple(
        PressureReport.model_validate(read_json(path, 16384))
        for path in (directory / "pressure").glob("final-*.json")
    )
    if (
        len(primary) != 9
        or len(final) != 12
        or not all(item.resources_closed for item in (*primary, *final))
    ):
        raise EvidenceFailure("pressure closure reports are missing or unresolved")
    enospc = tuple(item for item in primary if item.payload_enospc)
    expected_fs = "ext4" if report.platform == "linux" else "hfs"
    if (
        len(enospc) != 1
        or not enospc[0].metadata_enospc
        or enospc[0].payload_errno != errno.ENOSPC
        or enospc[0].metadata_headroom_bytes is None
        or not 0 < enospc[0].metadata_headroom_bytes <= 1048576
        or (
            enospc[0].metadata_errno != errno.ENOSPC
            and enospc[0].metadata_sqlite_code != sqlite3.SQLITE_FULL
        )
        or not enospc[0].unknown_state_charged
        or not enospc[0].cleanup_completed
        or not enospc[0].provider_started
        or enospc[0].failure is not None
        or enospc[0].filesystem != expected_fs
    ):
        raise EvidenceFailure("real platform ENOSPC/recovery evidence is incomplete")
    for failure in (
        "controller_control_closed",
        "controller_cancelled",
        "child_deadline",
        "child_failure",
    ):
        if (
            sum(item.failure == failure and item.provider_started for item in primary)
            != 1
        ):
            raise EvidenceFailure(
                "a required pressure failure/closure trace is missing"
            )
    return report.platform
