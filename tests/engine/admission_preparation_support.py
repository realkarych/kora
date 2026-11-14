import asyncio
import sys
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType

from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import Database
from kora.engine.admission_preparation import (
    AdmissionPreparationLimits,
    PreparedAdmissionSnapshot,
    prepare_admission_snapshot,
)
from kora.engine.attempt_capacity import prepare_graph_caps
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.readiness import prepare_readiness
from kora.engine.run_input_values import decode_run_input_value
from tests.engine.admission_snapshot_support import LIMITS as SNAPSHOT_LIMITS
from tests.engine.admission_snapshot_support import AdmissionSnapshotCase

LIMITS = AdmissionPreparationLimits(SNAPSHOT_LIMITS, 10**12, 10**22)
CHECKS = frozenset(
    function.__code__
    for function in (
        check_detached_execution_contract,
        prepare_readiness,
        prepare_graph_caps,
        decode_run_input_value,
        decode_claim_source,
    )
)


@contextmanager
def observe_detached_checks(database: Database) -> Generator[list[str]]:
    previous = sys.getprofile()
    observed: list[str] = []

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_code in CHECKS:
            assert database.units_in_flight() == 0
            observed.append(frame.f_code.co_name)

    try:
        sys.setprofile(observe)
        yield observed
    finally:
        sys.setprofile(previous)


async def prepare(
    database: Database,
    case: AdmissionSnapshotCase,
    *,
    limits: AdmissionPreparationLimits = LIMITS,
) -> PreparedAdmissionSnapshot:
    return await prepare_admission_snapshot(
        database,
        case.node_id,
        limits,
        observed_at=case.promotion.clock.now(),
        periodic_recheck=True,
        deadline=asyncio.get_running_loop().time() + 10.0,
    )
