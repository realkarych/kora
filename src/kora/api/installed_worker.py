import re
from dataclasses import dataclass
from datetime import timedelta

from kora.api.graph_deployment_limits import READ
from kora.db.claim_source_values import SourceAllowance
from kora.db.claim_sources import ClaimSourceReadLimits
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.db.grant_history import HistoryReadLimits
from kora.db.run_input_reads import RunInputReadLimits
from kora.engine.claim_snapshot import ClaimSnapshotLimits
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.engine.worker_claim_poll import ClaimPollLimits
from kora.protocols.deadline_records import MAX_UNCERTAINTY_US


@dataclass(frozen=True, slots=True)
class InstalledWorkerProfile:
    uncertainty: timedelta
    limits: ClaimPollLimits


def installed_worker_profile(value: str) -> InstalledWorkerProfile | None:
    if re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        return None
    microseconds = int(value)
    if microseconds > MAX_UNCERTAINTY_US:
        return None
    source = SourceAllowance(1024**2, 1024**2)
    limits = ClaimPollLimits(
        snapshot=ClaimSnapshotLimits(
            graph=READ,
            inputs=RunInputReadLimits(4096, MAX_PARAMS_BYTES, MAX_PARAMS_BYTES),
            sources=ClaimSourceReadLimits(
                run_row=1_000_000,
                node_row=4096,
                attempt_row=2 * 1024**2,
                fields=(source,) * len(SOURCE_FIELDS),
            ),
            graph_native_bytes=100_000_000,
            owner_text_bytes=4096,
            native_bytes=200_000_000,
            raw_bytes=200_000_000,
        ),
        history=HistoryReadLimits(
            65_536, 65_536, 65_536, 65_536, 100_000_000, 10_000_000
        ),
        expiry=PromotionReadLimits(1_000_000, 1_000_000),
        worker_bytes=65_536,
        read_seconds=10.0,
    )
    limits.snapshot.admit(extra_bytes=limits.replay_bytes)
    return InstalledWorkerProfile(timedelta(microseconds=microseconds), limits)
