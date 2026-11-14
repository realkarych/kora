import asyncio
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from types import FrameType
from typing import Literal

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings
from kora.engine.admission_preparation import (
    AdmissionPreparationRejected,
    PreparedAdmissionSnapshot,
    prepare_admission_snapshot,
)
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, WriterCutoffLoop
from tests.engine.admission_preparation_support import LIMITS, observe_detached_checks
from tests.engine.admission_snapshot_support import snapshot_case

__all__ = ["api_capture", "preparer"]

ObservationOutcome = Literal["success", "early", "acquisition", "deadline", "cancel"]


def observe_preparation_outcome(
    settings: DatabaseSettings,
    node_id: str,
    observed_at: datetime,
    outcome: ObservationOutcome,
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        database = Database(settings)
        ledger = ConnectionLedger(database)
        deadline = loop.time() + 10.0
        previous_trace = sys.gettrace()
        previous_profile = sys.getprofile()
        constructed = False
        prepared: PreparedAdmissionSnapshot | None = None
        original: BaseException | None = None
        failure: BaseException | None = None
        checks: list[str] = []
        if outcome == "acquisition":
            loop.advance_to(deadline)

        def completed(frame: FrameType, event: str, argument: object) -> None:
            nonlocal constructed
            if event != "return":
                return
            assert not constructed
            assert database.units_in_flight() == ledger.outstanding == 0
            constructed = True
            if outcome == "cancel":
                current = asyncio.current_task()
                assert current is not None
                current.cancel()
            else:
                loop.advance_to(deadline)

        def observe(
            frame: FrameType, event: str, argument: object
        ) -> Callable[[FrameType, str, object], None] | None:
            if (
                event == "call"
                and frame.f_code is PreparedAdmissionSnapshot.__init__.__code__
            ):
                frame.f_trace_lines = False
                return completed
            return None

        try:
            if outcome in {"deadline", "cancel"}:
                sys.settrace(observe)
            try:
                with observe_detached_checks(database) as checks:
                    try:
                        prepared = await prepare_admission_snapshot(
                            database,
                            node_id,
                            replace(LIMITS, work_units=1)
                            if outcome == "early"
                            else LIMITS,
                            observed_at=observed_at,
                            periodic_recheck=True,
                            deadline=deadline,
                        )
                    except (
                        AdmissionPreparationRejected,
                        asyncio.CancelledError,
                    ) as error:
                        original = error
                        raise
            except (AdmissionPreparationRejected, asyncio.CancelledError) as error:
                failure = error
            assert sys.getprofile() is previous_profile
        finally:
            sys.settrace(previous_trace)
            await database.close()

        assert sys.gettrace() is previous_trace
        assert sys.getprofile() is previous_profile
        assert (
            ledger.checkouts
            == ledger.opened
            == ledger.closed
            == int(outcome not in {"early", "acquisition"})
        )
        assert ledger.outstanding == database.units_in_flight() == 0
        assert constructed is (outcome in {"deadline", "cancel"})
        assert failure is original
        assert (prepared is not None) is (outcome == "success")
        if outcome == "success":
            assert failure is None
        elif outcome == "cancel":
            assert isinstance(failure, asyncio.CancelledError)
            assert not getattr(failure, "__notes__", ())
        else:
            assert isinstance(failure, AdmissionPreparationRejected)
            assert failure.__cause__ is failure.__context__ is None
            assert len(failure.__notes__) == 1
            note = failure.__notes__[0]
            assert len(note.encode("utf-8")) < 1024
            assert note.startswith("admission_preparation_observation elapsed_seconds=")
            assert f"final_guard_entered={int(outcome == 'deadline')}" in note
            assert f"final_deadline_branch_entered={int(outcome == 'deadline')}" in note
            assert "owned_units=0" in note
        assert checks == (
            []
            if outcome in {"early", "acquisition"}
            else [
                "check_detached_execution_contract",
                "prepare_readiness",
                "prepare_graph_caps",
                "decode_run_input_value",
                "decode_claim_source",
                "decode_claim_source",
            ]
        )

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "early", "acquisition", "deadline", "cancel"]
)
async def test_admission_observer_identifies_final_deadline_branch(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    outcome: ObservationOutcome,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    before = await case.promotion.snapshot(database)

    await asyncio.to_thread(
        observe_preparation_outcome,
        database.settings,
        case.node_id,
        case.promotion.clock.now(),
        outcome,
    )
    after = await case.promotion.snapshot(database)

    assert before == after
    assert database.units_in_flight() == 0
