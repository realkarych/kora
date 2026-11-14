import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import Connection, delete, event, insert, select, update
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.util.concurrency import await_

from kora.api.graph_deployment_limits import READ
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.claim_source_values import ClaimSourcesRejected, prepare_claim_sources
from kora.db.claim_sources import ClaimSourceBinding
from kora.db.connection import Database, Lifecycle
from kora.db.run_input_parent import INPUT_SENTINEL
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import (
    attempt_claim_read_admission,
    graph_version,
    graph_version_read_admission,
    node_instance,
    run,
    run_inputs,
)
from kora.engine.attempt_inputs import prepare_attempt_inputs
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.claim_snapshot import (
    AcquiredClaimSnapshot,
    ClaimSnapshotLimits,
    acquire_claim_snapshot,
    prepare_claim_snapshot,
)
from kora.engine.claim_sources import insert_claim_attempt, update_claim_sources
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.engine.price_initialization import PriceConfiguration
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from tests.api.deployment_cases import (
    evaluator,
    prepared,
    publish,
    seeded_namespace,
    skeleton_ir,
)
from tests.db.closing import ConnectionLedger, narrow_database, until
from tests.db.seed import START, FakeClock, Seeder
from tests.engine.test_admission_initialization import initializer, rates
from tests.engine.test_claim_sources import (
    ALLOWANCE,
    source_values,
)
from tests.engine.test_claim_sources import (
    LIMITS as SOURCE_LIMITS,
)
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS

__all__ = ["evaluator"]
LIMITS = ClaimSnapshotLimits(
    READ, INPUT_LIMITS, SOURCE_LIMITS, 100_000_000, 4096, 200_000_000, 200_000_000
)
SELECTION = ClaimRuntimeSelection("local", "git", timedelta(microseconds=1_000_001))


async def seed(
    database: Database,
    evaluator: Evaluator,
    *,
    ir: JsonObject | None = None,
    node_path: str = "agent",
    versions: dict[str, str | None] | None = None,
    global_price: bool = False,
) -> tuple[ClaimSourceBinding, GraphVersionPin]:
    namespace_id, token_id = await seeded_namespace(database)
    deployment = await prepared(evaluator, ir)
    published = await publish(database, deployment, namespace_id, token_id)
    initialization = initializer(
        database,
        (
            NamespacePolicyConfiguration.prepare(
                namespace_id, 2, {"require_protected_execution": True}
            ),
        ),
        (PriceConfiguration(namespace_id, START, {"model": rates()}),),
    )
    with captured(
        "engine", LoggingSettings(), "initialize", FakeClock(START).now
    ) as capture:
        await initialization.initialize(capture.diagnostics)
    async with database.transaction() as writer:
        graph_id = await writer.scalar(
            select(graph_version.c.graph_id).where(
                graph_version.c.id == published.graph_version_id
            )
        )
        assert type(graph_id) is str
        seeder = Seeder(writer)
        values = seeder.run_values(namespace_id, graph_id, published.graph_version_id)
        values.update(
            input_encoding_version=1,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
        )
        await writer.execute(insert(run).values(**values))
        run_id = str(values["id"])
        await writer.execute(
            insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
        )
        node_id = await seeder.node(run_id, node_path)
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(kind="agent" if node_path == "agent" else "command")
        )
        binding = ClaimSourceBinding(
            RunInputBinding(namespace_id, graph_id, published.graph_version_id, run_id),
            node_id,
            seeder.new_id(),
            initialization.baseline.id if global_price else initialization.prices[0].id,
            node_path,
        )
        attempt_values: dict[str, object] = {
            "id": binding.attempt_id,
            "node_instance_id": node_id,
            "number": 1,
            "runtime": "local",
            "execution_state": "unknown",
            "recovery_version": 0,
            "recovery_required": False,
            "status": "ready",
            "desired_state": "run",
            "record_version": 1,
            "infrastructure_retries": 0,
            "lease_epoch": 0,
            "counters": {},
            "scratch": {},
            "price_table_id": binding.price_table_id,
            "created_at": START,
            "updated_at": START,
        }
        sources = source_values()
        declared: JsonObject = {}
        if ir is not None:
            graph = cast(JsonObject, ir["graph"])
            nodes = cast(JsonObject, graph["nodes"])
            declared = cast(
                JsonObject, cast(JsonObject, nodes[node_path]).get("inputs", {})
            )
        sources["input_bindings"] = prepare_attempt_inputs(
            declared, versions or {}
        ).storage
        prepared_sources = prepare_claim_sources(
            sources, {name: ALLOWANCE for name in sources}
        )
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        await insert_claim_attempt(
            writer,
            locks,
            binding,
            attempt_values,
            prepared_sources,
            SOURCE_LIMITS,
            START,
        )
    return binding, GraphVersionPin(
        graph_id,
        published.graph_version_id,
        deployment.inputs.logical.digests.execution_contract_digest,
    )


async def acquire(
    database: Database, binding: ClaimSourceBinding
) -> AcquiredClaimSnapshot:
    return await acquire_claim_snapshot(
        database, binding, LIMITS, deadline=asyncio.get_running_loop().time() + 10.0
    )


@pytest.mark.asyncio
async def test_claim_snapshot_composes_real_producers_after_one_checkout(
    database: Database, evaluator: Evaluator
) -> None:
    binding, pin = await seed(database, evaluator)
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)

    try:
        acquired = await acquire(reader, binding)
        assert ledger.outstanding == reader.units_in_flight() == 0
        result = prepare_claim_snapshot(acquired, pin, SELECTION, LIMITS)
    finally:
        await reader.close()

    immutable = ObjectReader(result.material.immutable.payload.decode()).object()
    assert ledger.checkouts == 1
    assert immutable["price_table_id"] == binding.price_table_id
    assert (
        cast(JsonObject, immutable["price_table"])["namespace_id"]
        == binding.run.namespace_id
    )
    assert immutable["params"] == immutable["input_bindings"] == {}
    assert immutable["limits"] == {"stall_after_us": "1000001"}
    assert result.sources.binding == binding
    assert result.sources.revision == 1
    assert result.policy_revision == 2
    assert (
        cast(JsonObject, immutable["execution_context"])["graph_version_id"]
        == pin.version_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["graph", "attempt", "params"])
async def test_claim_snapshot_refuses_missing_admission_or_inputs(
    database: Database, evaluator: Evaluator, missing: str
) -> None:
    binding, _ = await seed(database, evaluator)
    owner = {
        "graph": graph_version_read_admission,
        "attempt": attempt_claim_read_admission,
        "params": run_inputs,
    }[missing]
    async with database.transaction() as writer:
        await writer.execute(delete(owner))
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
        await acquire(database, binding)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_aggregate_admission_precedes_checkout(
    database: Database, evaluator: Evaluator
) -> None:
    binding, _ = await seed(database, evaluator)
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimSourcesRejected):
        await acquire_claim_snapshot(
            database,
            binding,
            replace(LIMITS, raw_bytes=1),
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_sealed_policy_survives_current_policy_change(
    database: Database, evaluator: Evaluator
) -> None:
    binding, pin = await seed(database, evaluator)
    initial = prepare_claim_snapshot(
        await acquire(database, binding), pin, SELECTION, LIMITS
    )
    saved = ObjectReader(initial.policy.payload.decode()).object()["execution_policy"]
    sources: JsonObject = {
        "execution_policy": saved,
        "execution_request": {"version": 1},
    }
    prepared_sources = prepare_claim_sources(
        sources, {name: ALLOWANCE for name in sources}
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        await update_claim_sources(
            writer, locks, binding, prepared_sources, SOURCE_LIMITS, 1, START
        )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(namespace_policy_scopes(binding.run.namespace_id))
        await write_namespace_policy(
            writer,
            locks,
            NamespacePolicyConfiguration.prepare(
                binding.run.namespace_id, 3, {"require_protected_execution": False}
            ),
            START,
        )

    acquired = await acquire(database, binding)
    result = prepare_claim_snapshot(acquired, pin, SELECTION, LIMITS)

    assert acquired.policy is None
    assert result.policy.payload == initial.policy.payload
    assert result.policy_revision is None
    assert result.sources.revision == 2


class PausedClaimRead:
    def __init__(self, database: Database) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.engine = database.reader_engine.sync_engine

    def __enter__(self) -> "PausedClaimRead":
        event.listen(self.engine, "before_cursor_execute", self.before)
        return self

    def __exit__(self, *arguments: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.before)

    def before(
        self,
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        *arguments: object,
    ) -> None:
        if "graph_version_read_admission" in statement and not self.entered.is_set():
            self.entered.set()
            await_(self.release.wait())


@pytest.mark.asyncio
async def test_claim_snapshot_cancellation_releases_the_only_reader(
    database: Database, evaluator: Evaluator
) -> None:
    binding, _ = await seed(database, evaluator)
    ledger = ConnectionLedger(database)
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(acquire(database, binding))
        await asyncio.wait_for(pause.entered.wait(), 10.0)

        pending.cancel()
        pause.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert ledger.outstanding == database.units_in_flight() == 0
    assert (await acquire(database, binding)).sources.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["parent", "projection", "params"])
@pytest.mark.parametrize("value", [-1_000_000_000, 0, True])
async def test_claim_snapshot_invalid_input_allowance_never_checks_out(
    database: Database, field: str, value: int
) -> None:
    identity = "01J00000000000000000000000"
    binding = ClaimSourceBinding(
        RunInputBinding(identity, identity, identity, identity),
        identity,
        identity,
        identity,
        "agent",
    )
    limits = replace(LIMITS, inputs=replace(INPUT_LIMITS, **{field: value}))
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimSourcesRejected):
        await acquire_claim_snapshot(
            database, binding, limits, deadline=asyncio.get_running_loop().time() + 10.0
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_observes_one_policy_and_source_revision(
    database: Database, evaluator: Evaluator
) -> None:
    binding, pin = await seed(database, evaluator)
    replacement = prepare_claim_sources(
        {"handle": {"runtime": "updated"}}, {"handle": ALLOWANCE}
    )
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(acquire(database, binding))
        await asyncio.wait_for(pause.entered.wait(), 10.0)
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await update_claim_sources(
                writer, locks, binding, replacement, SOURCE_LIMITS, 1, START
            )
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes(
                namespace_policy_scopes(binding.run.namespace_id)
            )
            await write_namespace_policy(
                writer,
                locks,
                NamespacePolicyConfiguration.prepare(
                    binding.run.namespace_id, 3, {"require_protected_execution": False}
                ),
                START,
            )

        pause.release.set()
        old = prepare_claim_snapshot(await pending, pin, SELECTION, LIMITS)
    new = prepare_claim_snapshot(
        await acquire(database, binding), pin, SELECTION, LIMITS
    )

    assert (old.sources.revision, old.policy_revision) == (1, 2)
    assert old.sources.values["handle"] is None
    assert (new.sources.revision, new.policy_revision) == (2, 3)
    assert new.sources.values["handle"] == {"runtime": "updated"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["node_id", "attempt_id", "price_table_id", "node_path"]
)
async def test_claim_snapshot_refuses_substituted_binding(
    database: Database, evaluator: Evaluator, field: str
) -> None:
    binding, _ = await seed(database, evaluator)
    substituted = replace(binding, **{field: "01J00000000000000000000000"})
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimSourcesRejected):
        await acquire(database, substituted)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_expired_deadline_never_checks_out(
    database: Database,
) -> None:
    identity = "01J00000000000000000000000"
    binding = ClaimSourceBinding(
        RunInputBinding(identity, identity, identity, identity),
        identity,
        identity,
        identity,
        "agent",
    )
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimSourcesRejected):
        await acquire_claim_snapshot(
            database, binding, LIMITS, deadline=asyncio.get_running_loop().time() - 1.0
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_expiry_during_acquisition_returns_no_material(
    database: Database, evaluator: Evaluator
) -> None:
    binding, _ = await seed(database, evaluator)
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + 1.0
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(
            acquire_claim_snapshot(database, binding, LIMITS, deadline=deadline)
        )
        await asyncio.wait_for(pause.entered.wait(), 10.0)

        try:
            with pytest.raises(ClaimSourcesRejected):
                await pending
        finally:
            pause.release.set()

    assert ledger.outstanding == database.units_in_flight() == 0
    assert (await acquire(database, binding)).sources.revision == 1


@pytest.mark.asyncio
async def test_claim_snapshot_settles_during_graceful_shutdown(
    database: Database, evaluator: Evaluator
) -> None:
    binding, pin = await seed(database, evaluator)
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)
    with PausedClaimRead(reader) as pause:
        pending = asyncio.create_task(acquire(reader, binding))
        await asyncio.wait_for(pause.entered.wait(), 10.0)
        closing = asyncio.create_task(reader.close(10.0))
        try:
            await until(lambda: reader.lifecycle is Lifecycle.CLOSING)
        finally:
            pause.release.set()

        acquired = await pending
        await closing
        result = prepare_claim_snapshot(acquired, pin, SELECTION, LIMITS)

    assert result.sources.revision == 1
    assert reader.lifecycle is Lifecycle.CLOSED
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == reader.units_in_flight() == 0


class ClaimQueries:
    def __init__(self, database: Database) -> None:
        self.engine = database.reader_engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self) -> "ClaimQueries":
        event.listen(self.engine, "before_cursor_execute", self.before)
        return self

    def __exit__(self, *arguments: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.before)

    def before(
        self,
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        *arguments: object,
    ) -> None:
        self.statements.append(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["graph", "attempt"])
async def test_claim_snapshot_native_admission_precedes_payload_query(
    database: Database, evaluator: Evaluator, owner: str
) -> None:
    binding, _ = await seed(database, evaluator)
    limits = (
        replace(LIMITS, graph_native_bytes=1)
        if owner == "graph"
        else replace(
            LIMITS,
            sources=replace(
                SOURCE_LIMITS,
                fields=(replace(ALLOWANCE, native=1), *SOURCE_LIMITS.fields[1:]),
            ),
        )
    )
    ledger = ConnectionLedger(database)
    with ClaimQueries(database) as observed:
        with pytest.raises(ClaimSourcesRejected):
            await acquire_claim_snapshot(
                database,
                binding,
                limits,
                deadline=asyncio.get_running_loop().time() + 10.0,
            )

    assert not any(
        "AS ir" in query or "CAST(v.ir AS BLOB)" in query
        for query in observed.statements
    )
    assert not any(
        "FROM price_table" in query or "FROM ONLY price_table" in query
        for query in observed.statements
    )
    if owner == "attempt":
        assert not any(
            "CAST(input_bindings AS" in query for query in observed.statements
        )
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_preserves_named_artifact_and_optional_absence(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    nodes = cast(JsonObject, cast(JsonObject, ir["graph"])["nodes"])
    gate = cast(JsonObject, nodes["gate"])
    gate["inputs"] = {
        "summary": {"type": "StructuredOutput", "from": "agent", "output": "summary"},
        "optional_summary": {
            "type": "StructuredOutput",
            "from": "agent",
            "output": "summary",
            "optional": True,
        },
    }
    version = "01J00000000000000000000000"
    binding, pin = await seed(
        database,
        evaluator,
        ir=ir,
        node_path="gate",
        versions={"summary": version, "optional_summary": None},
    )

    result = prepare_claim_snapshot(
        await acquire(database, binding), pin, SELECTION, LIMITS
    )

    inputs = cast(
        JsonObject,
        ObjectReader(result.material.immutable.payload.decode()).object()[
            "input_bindings"
        ],
    )
    assert list(inputs) == ["summary", "optional_summary"]
    assert inputs["summary"] == {
        "source": {"kind": "artifact", "artifact_version_id": version},
        "path": None,
        "relation": "consumed",
    }
    assert inputs["optional_summary"] == {
        "source": {"kind": "absent"},
        "path": None,
        "relation": "consumed",
    }


@pytest.mark.asyncio
async def test_claim_snapshot_reads_the_pinned_empty_global_baseline(
    database: Database, evaluator: Evaluator
) -> None:
    binding, pin = await seed(database, evaluator, global_price=True)

    result = prepare_claim_snapshot(
        await acquire(database, binding), pin, SELECTION, LIMITS
    )

    price = cast(
        JsonObject,
        ObjectReader(result.material.immutable.payload.decode()).object()[
            "price_table"
        ],
    )
    assert price["id"] == binding.price_table_id
    assert price["namespace_id"] is None
    assert price["effective_from"] == "0001-01-01T00:00:00Z"
    assert price["prices"] == {}
