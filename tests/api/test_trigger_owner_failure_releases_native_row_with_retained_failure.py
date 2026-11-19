import asyncio
import gc
import sys
from types import FrameType
from typing import cast

import pytest
from sqlalchemy import Row, insert, text

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.run_trigger_route import manual_start_preparation
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.graph_resource_owner_reads import acquire_graph_resource_owners
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.tables import graph_version_resource_owner
from kora.engine.start_acceptance import StartCandidate, select_manual_start
from kora.engine.start_replay import StartReadFailed
from tests.api.deployment_cases import HARNESS, evaluator, preparer
from tests.api.support import deployment_services
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_version_resource_owner_migration import owner_values
from tests.engine.start_cases import KEY, deploy, request_for

__all__ = ["evaluator", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native_error", [False, True])
@pytest.mark.parametrize("cancel_rollback", [False, True])
async def test_trigger_owner_failure_releases_native_row_with_retained_failure(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    native_error: bool,
    cancel_rollback: bool,
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    candidate = await select_manual_start(database, case.user, request, KEY)
    assert isinstance(candidate, StartCandidate)
    async with database.transaction() as writer:
        if native_error:
            await writer.execute(text("DROP TABLE graph_version_resource_owner"))
        else:
            await writer.execute(
                insert(graph_version_resource_owner).values(
                    owner_values(case.version_id) | {"ordinal": 1}
                )
            )
    row_ids: list[int] = []
    cancelled = False
    ledger = ConnectionLedger(database)

    def observe(frame: FrameType, event: str, _: object) -> None:
        nonlocal cancelled
        if (
            not row_ids
            and event == "call"
            and frame.f_code is acquire_graph_resource_owners.__code__
        ):
            parent = frame.f_back
            assert parent is not None
            row = parent.f_locals["row"]
            assert isinstance(row, Row)
            assert gc.is_tracked(row)
            row_ids.append(id(cast(object, row)))
        if (
            cancel_rollback
            and row_ids
            and not cancelled
            and event == "call"
            and frame.f_code.co_name == "_rollback_unit"
            and frame.f_globals.get("__name__") == "kora.db.connection"
        ):
            task = asyncio.current_task()
            assert task is not None
            cancelled = True
            task.cancel()

    incumbent = sys.getprofile()
    prepare = manual_start_preparation(
        deployment_services(database, preparer, HARNESS), request, KEY
    )

    sys.setprofile(observe)
    try:
        expected = (
            asyncio.CancelledError
            if cancel_rollback
            else StartReadFailed
            if native_error
            else GraphValueRejected
        )
        with pytest.raises(expected) as failure:
            await prepare(candidate)
    finally:
        sys.setprofile(incumbent)
    async with preparer.hold():
        released = not any(
            isinstance(value, Row) and id(cast(object, value)) in row_ids
            for value in gc.get_objects()
        )

    assert row_ids
    assert cancelled is cancel_rollback
    if isinstance(failure.value, GraphValueRejected):
        assert failure.value.reason is GraphValueFailure.INVALID
    assert failure.value.__traceback__ is not None
    assert failure.value.__cause__ is failure.value.__context__ is None
    assert released
    assert ledger.outstanding == database.units_in_flight() == 0
