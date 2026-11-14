import asyncio
import sys
from collections.abc import Generator
from contextlib import contextmanager
from time import monotonic
from types import FrameType

from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import (
    Database,
    DatabaseSnapshotRejected,
    require_snapshot_time,
)
from kora.engine.admission_preparation import (
    AdmissionPreparationLimits,
    AdmissionPreparationRejected,
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
CHECK_CODE_IDS: frozenset[int] = frozenset(
    id(function.__code__)
    for function in (
        check_detached_execution_contract,
        prepare_readiness,
        prepare_graph_caps,
        decode_run_input_value,
        decode_claim_source,
    )
)
FINAL_GUARD_CODE_ID: int = id(require_snapshot_time.__code__)
PREPARATION_CODE_ID: int = id(prepare_admission_snapshot.__code__)
SNAPSHOT_REFUSAL_CODE_ID: int = id(DatabaseSnapshotRejected.__init__.__code__)


@contextmanager
def observe_detached_checks(database: Database) -> Generator[list[str]]:
    previous = sys.getprofile()
    observed: list[str] = []
    final_guard_entered = False
    final_deadline_branch_entered = False

    def observe(frame: FrameType, event: str, argument: object) -> None:
        nonlocal final_guard_entered, final_deadline_branch_entered
        if event != "call":
            return
        code_id = id(frame.f_code)
        if code_id in CHECK_CODE_IDS:
            assert database.units_in_flight() == 0
            observed.append(frame.f_code.co_name)
        elif code_id == FINAL_GUARD_CODE_ID:
            caller = frame.f_back
            if caller is not None and id(caller.f_code) == PREPARATION_CODE_ID:
                final_guard_entered = True
        elif code_id == SNAPSHOT_REFUSAL_CODE_ID:
            caller = frame.f_back
            if caller is not None and id(caller.f_code) == FINAL_GUARD_CODE_ID:
                owner = caller.f_back
                if owner is not None and id(owner.f_code) == PREPARATION_CODE_ID:
                    final_deadline_branch_entered = True

    started = monotonic()
    try:
        try:
            sys.setprofile(observe)
            yield observed
        finally:
            sys.setprofile(previous)
    except AdmissionPreparationRejected as error:
        elapsed = monotonic() - started
        error.add_note(
            "admission_preparation_observation "
            f"elapsed_seconds={elapsed:.6e} "
            f"entered_count={len(observed):d} "
            f"last_entered={','.join(observed[-8:])} "
            f"owned_units={database.units_in_flight():d} "
            f"final_guard_entered={int(final_guard_entered):d} "
            f"final_deadline_branch_entered={int(final_deadline_branch_entered):d}"
        )
        raise


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
