import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import LargeBinary, Text, cast, func, select

from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.cost_reads import read_latest_price
from kora.db.identifiers import IdentifierGenerator
from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT, native_value_bound
from kora.db.tables import namespace, price_table
from kora.db.transactions import WriterTransactionError
from kora.db.types import JsonObject, JsonValue
from kora.engine.admission_initialization import (
    AdmissionInitialization,
    AdmissionInitializationRefused,
)
from kora.engine.locks import COMPONENT, LockError, TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    NamespacePolicyRefused,
    namespace_policy_scopes,
    running_attempt_limit,
    write_namespace_policy,
)
from kora.engine.price_initialization import (
    EARLIEST_PRICE_TIME,
    PriceConfiguration,
    PriceInitializationRefused,
)
from kora.ir.cost import PriceRates
from kora.observability import LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.engine.lock_support import (
    DEADLINE,
    held_transaction,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.plugins.authentication.support import AuthorityClock


async def namespace_id(database: Database) -> str:
    async with database.transaction() as connection:
        return await Seeder(connection).namespace()


def initializer(
    database: Database,
    policies: tuple[NamespacePolicyConfiguration, ...] = (),
    prices: tuple[PriceConfiguration, ...] = (),
    clock: AuthorityClock | None = None,
) -> AdmissionInitialization:
    clock = clock or AuthorityClock()
    return AdmissionInitialization(
        database,
        clock,
        IdentifierGenerator(clock.now).new,
        policies=policies,
        prices=prices,
    )


def rates(value: str = "1.25") -> PriceRates:
    return PriceRates(
        input=value,
        cached_input=None,
        cache_creation_5m=None,
        cache_creation_1h=None,
        output=None,
        reasoning=None,
    )


async def prices_in(database: Database) -> list[tuple[str, datetime]]:
    async with database.read_transaction() as connection:
        result = await connection.execute(
            select(price_table.c.id, price_table.c.effective_from).order_by(
                price_table.c.effective_from
            )
        )
        return [(row.id, row.effective_from) for row in result]


@pytest.mark.asyncio
async def test_initialization_commits_policy_prices_and_replays_retained_ids(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    policy: JsonObject = {
        "concurrency": {"max_pending_runs": 13, "max_running_attempts": 2},
        "private": {"retained": ["policy-content", 1e23, True]},
    }
    configuration = NamespacePolicyConfiguration.prepare(identity, 2, policy)
    configured_price = PriceConfiguration(identity, START, {"model": rates()})
    first = initializer(database, (configuration,), (configured_price,))
    second = initializer(database, (configuration,), (configured_price,))

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await first.initialize(capture.diagnostics)
        saved = await prices_in(database)
        await second.initialize(capture.diagnostics)

    assert await prices_in(database) == saved
    assert saved == [
        (first.baseline.id, EARLIEST_PRICE_TIME),
        (first.prices[0].id, START),
    ]
    async with database.read_transaction() as connection:
        row = (await connection.execute(select(namespace))).one()
        assert row.policy_revision == 2
        assert row.policy["private"]["retained"][0] == "policy-content"
        assert running_attempt_limit(row.policy) == 2
        price = await read_latest_price(connection, identity, START)
        assert price is not None
        assert price.id == first.prices[0].id
        assert price.prices["model"] == rates()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_revision", [1, 2])
async def test_policy_revision_refusal_rolls_back_all_configuration(
    database: Database, new_revision: int
) -> None:
    identity = await namespace_id(database)
    current = NamespacePolicyConfiguration.prepare(identity, 2, {"private": "current"})
    initial = initializer(database, (current,))
    conflicting = NamespacePolicyConfiguration.prepare(
        identity, new_revision, {"private": "conflicting"}
    )
    conflicting_init = initializer(
        database,
        (conflicting,),
        (PriceConfiguration(None, START, {"model": rates()}),),
    )
    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await initial.initialize(capture.diagnostics)
        saved = await prices_in(database)

        with pytest.raises(AdmissionInitializationRefused) as failure:
            await conflicting_init.initialize(capture.diagnostics)

    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert await prices_in(database) == saved
    async with database.read_transaction() as connection:
        row = (await connection.execute(select(namespace))).one()
        assert row.policy_revision == 2
        assert row.policy == {"private": "current"}


@pytest.mark.asyncio
async def test_price_conflict_rolls_back_earlier_policy_write(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    first = initializer(
        database, prices=(PriceConfiguration(None, START, {"m": rates()}),)
    )
    conflict = initializer(
        database,
        (NamespacePolicyConfiguration.prepare(identity, 2, {}),),
        (PriceConfiguration(None, START, {"m": rates("2")}),),
    )
    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await first.initialize(capture.diagnostics)
        saved = await prices_in(database)

        with pytest.raises(AdmissionInitializationRefused):
            await conflict.initialize(capture.diagnostics)

    assert await prices_in(database) == saved
    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 1


@pytest.mark.asyncio
async def test_concurrent_initializers_preserve_configured_earliest_default(
    database: Database,
) -> None:
    configured = PriceConfiguration(None, EARLIEST_PRICE_TIME, {"m": rates()})
    first = initializer(database, prices=(configured,))
    second = initializer(database, prices=(configured,))
    clock = AuthorityClock(START - timedelta(days=3650))
    replay = initializer(database, clock=clock)

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await asyncio.gather(
            first.initialize(capture.diagnostics),
            second.initialize(capture.diagnostics),
        )
        saved = await prices_in(database)
        await replay.initialize(capture.diagnostics)

    assert len(saved) == 1
    assert saved[0][0] in {first.prices[0].id, second.prices[0].id}
    assert await prices_in(database) == saved
    async with database.read_transaction() as connection:
        found = await read_latest_price(connection, None, clock.now())
        assert found is not None
        assert found.id == saved[0][0]
        assert found.prices["m"] == rates()


@pytest.mark.asyncio
async def test_year_one_prices_round_trip_in_chronological_order(
    database: Database,
) -> None:
    moments = tuple(datetime(year, 1, 1, tzinfo=UTC) for year in (1, 9, 99, 999, 1000))
    service = initializer(
        database,
        prices=tuple(
            PriceConfiguration(None, moment, {}) for moment in reversed(moments)
        ),
    )

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await service.initialize(capture.diagnostics)

    assert tuple(moment for _, moment in await prices_in(database)) == moments
    async with database.read_transaction() as connection:
        for moment in moments:
            price = await read_latest_price(connection, None, moment)
            assert price is not None
            assert price.effective_from == moment
        raw = await connection.scalar(
            select(cast(price_table.c.effective_from, Text))
            .order_by(price_table.c.effective_from)
            .limit(1)
        )
        assert raw is not None
        assert raw.startswith("0001-01-01")


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['雪\n"\\' * 500, 1e308, -(10**400)])
async def test_policy_native_allowance_bounds_database_and_transport_expansion(
    database: Database, content: str | int | float
) -> None:
    identity = await namespace_id(database)
    policy: JsonObject = {"private": content}
    bound = native_value_bound(policy)
    service = initializer(
        database, (NamespacePolicyConfiguration.prepare(identity, 2, policy),)
    )

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await service.initialize(capture.diagnostics)

    async with database.read_transaction() as connection:
        if connection.dialect.name == "postgresql":
            measured = await connection.execute(
                select(
                    func.pg_column_size(namespace.c.policy),
                    func.octet_length(cast(namespace.c.policy, Text)),
                )
            )
        else:
            measured = await connection.execute(
                select(func.length(cast(namespace.c.policy, LargeBinary)))
            )
        assert all(
            0 < width <= bound <= NAMESPACE_POLICY_NATIVE_LIMIT
            for width in measured.one()
        )


@pytest.mark.parametrize(
    "policy",
    [
        None,
        [],
        {"concurrency": None},
        {"concurrency": {"max_running_attempts": None}},
        {"concurrency": {"max_running_attempts": True}},
        {"concurrency": {"max_running_attempts": 1.0}},
        {"concurrency": {"max_running_attempts": 0}},
        {"concurrency": {"max_pending_runs": -1}},
        {"private": float("inf")},
        {"private": "\x00"},
        {"private": "x" * 5500},
        {"private": START},
    ],
)
def test_policy_preparation_refuses_invalid_complete_values(policy: object) -> None:
    identity = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    with pytest.raises(NamespacePolicyRefused):
        NamespacePolicyConfiguration.prepare(identity, 2, policy)


@pytest.mark.asyncio
async def test_namespace_writer_replays_without_a_second_write(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    policy = NamespacePolicyConfiguration.prepare(identity, 2, {})

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(namespace_policy_scopes(identity))
        changed = await write_namespace_policy(connection, locks, policy, START)
        replay = await write_namespace_policy(connection, locks, policy, START)

    assert changed is True
    assert replay is False


@pytest.mark.asyncio
async def test_policy_at_native_budget_replays_without_rewriting(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    policy: JsonObject = {"a": "x" * 5430, "b": True}
    configuration = NamespacePolicyConfiguration.prepare(identity, 2, policy)
    service = initializer(database, (configuration,))

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await service.initialize(capture.diagnostics)
        await service.initialize(capture.diagnostics)
        with pytest.raises(NamespacePolicyRefused):
            NamespacePolicyConfiguration.prepare(identity, 3, {**policy, "c": True})

    assert native_value_bound(policy) == NAMESPACE_POLICY_NATIVE_LIMIT
    async with database.read_transaction() as connection:
        row = (await connection.execute(select(namespace))).one()
        assert row.policy_revision == 2
        assert row.policy == policy


@pytest.mark.asyncio
async def test_deep_policy_within_native_budget_survives_replay(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    nested: JsonValue = {}
    for _ in range(600):
        nested = [nested]
    policy: JsonObject = {"a": nested}
    configuration = NamespacePolicyConfiguration.prepare(identity, 2, policy)
    service = initializer(database, (configuration,))

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await service.initialize(capture.diagnostics)
        await service.initialize(capture.diagnostics)

    assert native_value_bound(policy) <= NAMESPACE_POLICY_NATIVE_LIMIT
    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 2


@pytest.mark.asyncio
async def test_cancelled_policy_writer_rolls_back_and_releases_locks(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    policy = NamespacePolicyConfiguration.prepare(identity, 2, {})
    written = asyncio.Event()
    held = asyncio.Event()
    ledger = ConnectionLedger(database)

    async def write_until_cancelled() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_scopes(namespace_policy_scopes(identity))
            await write_namespace_policy(connection, locks, policy, START)
            written.set()
            await asyncio.wait_for(held.wait(), DEADLINE)

    task = asyncio.create_task(write_until_cancelled())
    try:
        await asyncio.wait_for(written.wait(), DEADLINE)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        held.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 1
    assert await lock_inventory(database) == []
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancelled_initializer_waiting_for_policy_owner_has_no_effect(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    configured = NamespacePolicyConfiguration.prepare(identity, 2, {})
    service = initializer(database, (configured,))
    entered = asyncio.Event()
    release = asyncio.Event()
    owner = asyncio.create_task(
        held_transaction(database, entered, release, namespace_policy_scopes(identity))
    )
    ledger = ConnectionLedger(database)
    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), DEADLINE)
            task = asyncio.create_task(service.initialize(capture.diagnostics))
            await wait_for_lock_waiter(database)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(owner, DEADLINE)

    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 1
        assert (await connection.execute(select(price_table.c.id))).all() == []
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_policy_writer_refuses_wrong_connection_task_and_released_owner(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    configured = NamespacePolicyConfiguration.prepare(identity, 2, {})

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(namespace_policy_scopes(identity))
        async with database.read_transaction() as reader:
            with pytest.raises(LockError, match="does not own"):
                await write_namespace_policy(reader, locks, configured, START)
        with pytest.raises(WriterTransactionError, match="not current"):
            await asyncio.create_task(
                write_namespace_policy(connection, locks, configured, START)
            )
    async with database.transaction() as connection:
        with pytest.raises(WriterTransactionError, match="not current"):
            await write_namespace_policy(connection, locks, configured, START)

    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 1
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_configuration_is_snapshotted_before_the_writer_starts(
    database: Database,
) -> None:
    identity = await namespace_id(database)
    policy: JsonObject = {"concurrency": {"max_running_attempts": 2}}
    prices = {"model": rates()}
    configured = NamespacePolicyConfiguration.prepare(identity, 2, policy)
    service = initializer(
        database, (configured,), (PriceConfiguration(None, START, prices),)
    )
    policy["concurrency"] = {"max_running_attempts": 99}
    prices["model"] = rates("99")

    with captured(
        COMPONENT, LoggingSettings(), "initialize", AuthorityClock().now
    ) as capture:
        await service.initialize(capture.diagnostics)

    async with database.read_transaction() as connection:
        stored = await connection.scalar(select(namespace.c.policy))
        price = await read_latest_price(connection, None, START)
    assert running_attempt_limit(stored) == 2
    assert price is not None
    assert price.prices["model"] == rates()


@pytest.mark.asyncio
async def test_oversized_price_configuration_is_refused_before_writer_acquisition(
    database: Database,
) -> None:
    configuration = PriceConfiguration(
        None, START, {f"model-{number}": rates() for number in range(1000)}
    )
    ledger = ConnectionLedger(database)

    with pytest.raises(PriceInitializationRefused) as failure:
        initializer(database, prices=(configuration,))

    assert failure.value.__context__ is None
    assert ledger.checkouts == 0
    assert database.units_in_flight() == 0
