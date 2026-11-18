from datetime import timedelta

import pytest
from sqlalchemy import insert, select

from kora.db.attempt_capacity import RunRecovery
from kora.db.connection import Database
from kora.db.identifiers import encode
from kora.db.native_values import native_value_bound
from kora.db.tables import node_instance, wait
from kora.db.types import canonical_json
from kora.engine.admission_wait_reason import (
    MAX_OBSERVED_COUNT,
    MAX_REASON_BYTES,
    MAX_REASON_NATIVE_UNITS,
    AdmissionReasonRejected,
    admission_wait_reason,
)
from kora.engine.attempt_capacity import CapacityLimit
from kora.engine.expiry import close_expired_waits
from kora.engine.graph_resource_references import GraphResourceScope
from tests.db.seed import START
from tests.engine.expiry_support import expiry_context
from tests.engine.promotion_support import promotion_case

SCOPE = GraphResourceScope(encode(1), encode(2), encode(3))
OBSERVATIONS: tuple[CapacityLimit | RunRecovery, ...] = (
    CapacityLimit(
        "global", "max_running_attempts", MAX_OBSERVED_COUNT, MAX_OBSERVED_COUNT, None
    ),
    CapacityLimit(
        "namespace",
        "max_running_attempts",
        MAX_OBSERVED_COUNT,
        MAX_OBSERVED_COUNT,
        None,
    ),
    CapacityLimit(
        "graph", "max_running_nodes", MAX_OBSERVED_COUNT, MAX_OBSERVED_COUNT, encode(3)
    ),
    CapacityLimit(
        "graph", "max_running_runs", MAX_OBSERVED_COUNT, MAX_OBSERVED_COUNT, encode(3)
    ),
    CapacityLimit(
        "runtime", "max_concurrent", MAX_OBSERVED_COUNT, MAX_OBSERVED_COUNT, None
    ),
    RunRecovery(MAX_OBSERVED_COUNT, tuple(encode(value) for value in range(20))),
)


@pytest.mark.parametrize(
    ("observation", "canonical_bytes", "native_units"),
    tuple(
        zip(
            OBSERVATIONS,
            (247, 274, 291, 290, 750, 736),
            (3542, 3890, 4118, 4106, 6542, 10516),
            strict=True,
        )
    ),
)
def test_admission_reason_maximal_shapes_fit_closed_representation(
    observation: CapacityLimit | RunRecovery, canonical_bytes: int, native_units: int
) -> None:
    runtime = "\\" * 255

    reason = admission_wait_reason(observation, SCOPE, runtime, START)

    assert (
        len(canonical_json(reason).encode("utf-8"))
        == canonical_bytes
        <= MAX_REASON_BYTES
    )
    assert native_value_bound(reason) == native_units <= MAX_REASON_NATIVE_UNITS


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", OBSERVATIONS)
async def test_admission_reason_round_trips_and_survives_wait_closure(
    database: Database, observation: CapacityLimit | RunRecovery
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    reason = admission_wait_reason(
        observation, case.scope, "\\" * 255, case.clock.now()
    )
    observed_at = case.clock.now()
    closed_at = observed_at + timedelta(minutes=1)
    wait_id = case.identifiers.new()
    async with database.transaction() as writer:
        node_id = await writer.scalar(
            select(node_instance.c.id)
            .where(node_instance.c.run_id == case.run_id)
            .order_by(node_instance.c.id)
            .limit(1)
        )
        assert isinstance(node_id, str)
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=node_id,
                attempt_id=None,
                kind="admission",
                wake_at=observed_at + timedelta(seconds=1),
                reason=reason,
                status="open",
                created_at=observed_at,
                updated_at=observed_at,
            )
        )

    async with database.transaction() as writer:
        context = await expiry_context(writer, case)
        await close_expired_waits(context, closed_at, node_id)
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait).where(wait.c.id == wait_id)) as result:
            saved = result.mappings().one()

    assert saved["reason"] == reason
    assert reason["observed_at"] == observed_at.isoformat(timespec="microseconds")
    assert saved["status"] == "cancelled"
    assert saved["updated_at"] == closed_at
    assert saved["attempt_id"] is None


@pytest.mark.parametrize("count", [1, 20, 21, MAX_OBSERVED_COUNT])
def test_admission_recovery_reason_preserves_exact_count_and_bounded_sample(
    count: int,
) -> None:
    sample = tuple(encode(value) for value in range(min(count, 20)))

    reason = admission_wait_reason(RunRecovery(count, sample), SCOPE, "local", START)

    assert reason["blockers"] == {
        "count": count,
        "ids": list(sample),
        "truncated": count > 20,
    }
    assert set(reason) == {"version", "kind", "observed_at", "blockers"}


@pytest.mark.parametrize(
    "observation",
    [
        RunRecovery(0, ()),
        RunRecovery(2, (encode(1),)),
        RunRecovery(2, (encode(1), encode(1))),
        RunRecovery(2, (encode(2), encode(1))),
        RunRecovery(1, ("invalid",)),
        RunRecovery(1, ("X" * 1_000_000,)),
        RunRecovery(MAX_OBSERVED_COUNT + 1, tuple(encode(i) for i in range(20))),
        CapacityLimit("global", "max_running_nodes", 1, 1, None),
        CapacityLimit("global", "max_running_attempts", 2, 1, None),
        CapacityLimit("global", "max_running_attempts", True, 1, None),
        CapacityLimit("global", "max_running_attempts", 1, 0, None),
        CapacityLimit(
            "global", "max_running_attempts", 1, MAX_OBSERVED_COUNT + 1, None
        ),
        CapacityLimit("global", "max_running_attempts", 1, 1, encode(3)),
        CapacityLimit("graph", "max_running_nodes", 1, 1, None),
        CapacityLimit("graph", "max_running_nodes", 1, 1, "invalid"),
    ],
)
def test_admission_reason_refuses_invalid_observation(
    observation: CapacityLimit | RunRecovery,
) -> None:
    with pytest.raises(AdmissionReasonRejected):
        admission_wait_reason(observation, SCOPE, "local", START)


@pytest.mark.parametrize("runtime", ["", "two words", "é", "r" * 256, "runtime\n"])
def test_admission_reason_uses_the_existing_runtime_name_domain(runtime: str) -> None:
    observation = CapacityLimit("runtime", "max_concurrent", 1, 1, None)

    with pytest.raises(AdmissionReasonRejected):
        admission_wait_reason(observation, SCOPE, runtime, START)
