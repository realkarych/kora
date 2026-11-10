import pickle
from contextlib import closing
from hashlib import sha256
from traceback import walk_tb
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    attach_graph_deployment_references,
)
from kora.api.graph_execution_preparation import PreparedGraphDeploymentInputs
from kora.api.graph_publication_owners import (
    PreparedGraphOwnerStatements,
    prepare_graph_publication_owners,
)
from kora.db.connection import Database, Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.identifiers import encode
from kora.db.tables import graph_version_resource_owner as owner_table
from kora.engine.graph_command_manifests import MAX_FILES, CommandResourceContent
from kora.engine.graph_resource_references import GraphResourceOwner, GraphResourceScope
from tests.api.test_graph_deployment_attachment import (
    ATTACH_LIMITS,
    PRIVATE_OBJECT,
    PRIVATE_STORE,
    prepare_inputs,
)
from tests.api.test_graph_execution_preparation import deployment_source
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.graph_output_cases import child
from tests.engine.test_graph_command_manifests import (
    ZERO,
    authored_manifest,
    canonical,
    command_document,
    resource,
)
from tests.engine.test_graph_resource_references import (
    BODY,
    ROOT,
    assert_refused,
    content,
    physical_file,
)

PAYLOAD = b"source bytes"
PAYLOAD_DIGEST = "sha256:" + sha256(PAYLOAD).hexdigest()
OWNER_LIMIT = 10_000
TARGET = (encode(1), encode(2), encode(3))


@pytest.fixture(scope="module")
def owner_preparations() -> tuple[
    PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
]:
    shared = resource("a.sh", len(PAYLOAD), PAYLOAD_DIGEST)
    raw = command_document(
        authored_manifest([resource("é.sh"), shared]), authored_manifest([shared])
    )
    files = (
        CommandResourceContent(BODY, "a.sh", PAYLOAD),
        CommandResourceContent(ROOT, "é.sh", b""),
        CommandResourceContent(ROOT, "a.sh", PAYLOAD),
    )
    return prepare_inputs(canonical(raw), files), prepare_inputs(
        deployment_source(()), ()
    )


def attached(
    prepared: PreparedGraphDeploymentInputs, target: tuple[str, str, str]
) -> AttachedGraphDeployment:
    scope = GraphResourceScope(*target)
    shared = content(
        store=PRIVATE_STORE,
        incarnation=PRIVATE_OBJECT,
        digest=PAYLOAD_DIGEST,
        size=len(PAYLOAD),
    )
    empty = content(store=PRIVATE_STORE, incarnation="empty-object")
    owners = (
        GraphResourceOwner(scope, BODY, "a.sh", shared),
        GraphResourceOwner(scope, ROOT, "é.sh", empty),
        GraphResourceOwner(scope, ROOT, "a.sh", shared),
    )
    return attach_graph_deployment_references(
        prepared, scope, owners, limits=ATTACH_LIMITS
    )


def expected(version_id: str) -> list[dict[str, str | int | None]]:
    return [
        {
            "graph_version_id": version_id,
            "ordinal": ordinal,
            "subgraph_name": body,
            "node_name": "producer",
            "file_name": name,
            "store_id": PRIVATE_STORE,
            "payload_digest": digest,
            "object_incarnation": incarnation,
            "total_size": size,
            "payload_state": "present",
        }
        for ordinal, (body, name, digest, incarnation, size) in enumerate(
            (
                (None, "a.sh", PAYLOAD_DIGEST, PRIVATE_OBJECT, len(PAYLOAD)),
                (None, "é.sh", ZERO, "empty-object", 0),
                ("body", "a.sh", PAYLOAD_DIGEST, PRIVATE_OBJECT, len(PAYLOAD)),
            )
        )
    ]


def test_owner_parameters_remain_bound_to_issued_scope_and_native_values(
    owner_preparations: tuple[
        PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
    ],
) -> None:
    prepared, empty = owner_preparations
    candidate = attached(prepared, TARGET)
    empty_candidate = attach_graph_deployment_references(
        empty, GraphResourceScope(*TARGET), (), limits=ATTACH_LIMITS
    )
    malformed_target = (TARGET[0], TARGET[1], "8" + "0" * 25)
    malformed_candidate = attach_graph_deployment_references(
        empty, GraphResourceScope(*malformed_target), (), limits=ATTACH_LIMITS
    )
    intended = expected(TARGET[2])
    exact = 1 + sum(
        len(row)
        + 2
        + sum(
            4 * len(value) if type(value) is str else 8 if type(value) is int else 0
            for value in row.values()
        )
        for row in intended
    )
    physical = candidate.resources.text
    changed_target = (TARGET[0], TARGET[1], encode(4))

    first = candidate.owner_parameters(TARGET, max_bind_units=exact)
    first[0]["object_incarnation"] = "substituted"
    candidate.owners[0].reference.address.storage_identity.__init__(
        version=1, store_id="substituted-store"
    )
    candidate.owners[1].__init__(
        candidate.scope, ROOT, "substituted.sh", candidate.owners[0].reference
    )
    candidate.scope.__init__(*changed_target)
    same = candidate.owner_parameters(TARGET, max_bind_units=exact)
    no_rows = empty_candidate.owner_parameters(TARGET, max_bind_units=1)
    refusals = (
        (candidate, TARGET, exact - 1, GraphValueFailure.BUDGET),
        (empty_candidate, TARGET, True, GraphValueFailure.BUDGET),
        (malformed_candidate, malformed_target, 1, GraphValueFailure.INVALID),
        (candidate, changed_target, exact, GraphValueFailure.INVALID),
        (
            candidate,
            (encode(5), TARGET[1], TARGET[2]),
            exact,
            GraphValueFailure.INVALID,
        ),
        (
            candidate,
            (TARGET[0], encode(5), TARGET[2]),
            exact,
            GraphValueFailure.INVALID,
        ),
        (
            candidate,
            (TARGET[0], TARGET[1], PRIVATE_OBJECT),
            exact,
            GraphValueFailure.INVALID,
        ),
        (
            candidate,
            cast(tuple[str, str, str], list(TARGET)),
            exact,
            GraphValueFailure.INVALID,
        ),
        (prepared, TARGET, exact, GraphValueFailure.INVALID),
        (
            object.__new__(AttachedGraphDeployment),
            TARGET,
            exact,
            GraphValueFailure.INVALID,
        ),
    )
    for attachment, target, allowance, reason in refusals:
        with pytest.raises(GraphValueRejected) as refusal:
            AttachedGraphDeployment.owner_parameters(
                cast(AttachedGraphDeployment, attachment),
                target,
                max_bind_units=allowance,
            )
        assert_refused(refusal.value, reason)
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refusal.value.__traceback__)
        }
        assert set(frames["owner_parameters"]) == {"reason"}
        assert "_owner_parameters" not in frames
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(candidate)
    with pytest.raises(ValueError, match="not iterable") as encoded:
        jsonable_encoder(candidate)

    assert same == intended
    assert first != same
    assert no_rows == []
    assert candidate.resources.text is physical
    document = ObjectReader(physical).object()
    for row, (command_index, file_index) in zip(
        same, ((0, 0), (0, 1), (1, 0)), strict=True
    ):
        entry = physical_file(document, command=command_index, file=file_index)
        reference = child(entry, "storage_ref")
        address = child(reference, "address")
        assert entry["name"] == row["file_name"]
        assert reference["object_incarnation"] == row["object_incarnation"]
        assert reference["total_size"] == row["total_size"]
        assert address["payload_digest"] == row["payload_digest"]
        assert child(address, "storage_identity")["store_id"] == row["store_id"]
    assert PRIVATE_OBJECT not in repr(candidate)
    assert PRIVATE_OBJECT not in repr(encoded.value)


class FixtureRollback(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_prepared_owner_parameters_bind_natively_and_rollback_atomically(
    database: Database,
    owner_preparations: tuple[
        PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
    ],
) -> None:
    ledger = ConnectionLedger(database)
    prepared, empty = owner_preparations
    statement = owner_table.insert()
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        first_version = await seeder.graph_version(graph, 1)
        rollback_version = await seeder.graph_version(graph, 2)
    first_target = (namespace, graph, first_version)
    rollback_target = (namespace, graph, rollback_version)
    first = attached(prepared, first_target).owner_parameters(
        first_target, max_bind_units=OWNER_LIMIT
    )
    rolled = attached(prepared, rollback_target).owner_parameters(
        rollback_target, max_bind_units=OWNER_LIMIT
    )
    no_rows = attach_graph_deployment_references(
        empty, GraphResourceScope(*rollback_target), (), limits=ATTACH_LIMITS
    ).owner_parameters(rollback_target, max_bind_units=OWNER_LIMIT)
    selected = (
        select(owner_table)
        .where(owner_table.c.graph_version_id == first_version)
        .order_by(owner_table.c.ordinal)
    )
    rollback_rows = (
        select(owner_table.c.ordinal)
        .where(owner_table.c.graph_version_id == rollback_version)
        .order_by(owner_table.c.ordinal)
    )
    before_rollback: list[object] = []

    async def insert_then_abort() -> None:
        async with database.transaction() as connection:
            await connection.execute(statement, rolled)
            before_rollback.extend(
                (await connection.execute(rollback_rows)).scalars().all()
            )
            raise FixtureRollback()

    async with database.transaction() as connection:
        await connection.execute(statement, first)
        if no_rows:
            await connection.execute(statement, no_rows)
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(statement, first)
    with pytest.raises(FixtureRollback):
        await insert_then_abort()
    async with database.read_snapshot() as connection:
        saved = (await connection.execute(selected)).mappings().all()
        after_rollback = (await connection.execute(rollback_rows)).all()

    assert no_rows == []
    assert PRIVATE_OBJECT not in str(statement)
    assert [dict(row) for row in saved] == expected(first_version)
    assert before_rollback == [0, 1, 2]
    assert after_rollback == []
    assert ledger.outstanding == database.units_in_flight() == 0


async def native_owner_match(
    connection: AsyncConnection, statements: PreparedGraphOwnerStatements
) -> object:
    with closing(
        await connection.execute(statements.compare, statements.parameters())
    ) as result:
        return result.scalar_one_or_none()


async def owner_decoy_observations(
    connection: AsyncConnection,
    source_version: str,
    missing: PreparedGraphOwnerStatements,
    empty: PreparedGraphOwnerStatements,
) -> tuple[int, object, object]:
    sqlite = empty.dialect is Dialect.SQLITE
    target = {"version": missing.scope_ids[2]}
    nested = await connection.begin_nested()
    try:
        if sqlite:
            await connection.execute(
                text("""
                    CREATE TEMP TABLE graph_version_resource_owner AS
                    SELECT * FROM main.graph_version_resource_owner
                    WHERE graph_version_id = :source
                """),
                {"source": source_version},
            )
            await connection.execute(
                text("""
                    UPDATE temp.graph_version_resource_owner
                    SET graph_version_id = :version
                """),
                target,
            )
        else:
            await connection.exec_driver_sql("""
                CREATE TABLE graph_owner_decoy ()
                INHERITS (graph_version_resource_owner)
            """)
            await connection.execute(
                text("""
                    INSERT INTO graph_owner_decoy
                    SELECT * FROM ONLY graph_version_resource_owner
                    WHERE graph_version_id = :source
                """),
                {"source": source_version},
            )
            await connection.execute(
                text("UPDATE graph_owner_decoy SET graph_version_id = :version"), target
            )
        ordinary = await connection.execute(
            select(owner_table.c.ordinal).where(
                owner_table.c.graph_version_id == missing.scope_ids[2]
            )
        )
        with closing(ordinary):
            visible = len(ordinary.scalars().all())
        return (
            visible,
            await native_owner_match(connection, missing),
            await native_owner_match(connection, empty),
        )
    finally:
        await nested.rollback()


@pytest.mark.asyncio
async def test_native_owner_comparison_requires_the_complete_exact_set(
    database: Database,
    owner_preparations: tuple[
        PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
    ],
) -> None:
    ledger = ConnectionLedger(database)
    prepared, empty = owner_preparations
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph, 1)
        neighbor = await seeder.graph_version(graph, 2)
    target = (namespace, graph, version)
    neighbor_target = (namespace, graph, neighbor)
    attachment = attached(prepared, target)
    statements = prepare_graph_publication_owners(
        attachment, target, database.dialect, max_bind_units=OWNER_LIMIT
    )
    empty_statement = prepare_graph_publication_owners(
        attach_graph_deployment_references(
            empty, GraphResourceScope(*neighbor_target), (), limits=ATTACH_LIMITS
        ),
        neighbor_target,
        database.dialect,
        max_bind_units=1,
    )
    missing = prepare_graph_publication_owners(
        attached(prepared, neighbor_target),
        neighbor_target,
        database.dialect,
        max_bind_units=OWNER_LIMIT,
    )
    scope = owner_table.c.graph_version_id == version
    root = scope & (owner_table.c.ordinal == 1)
    body = scope & (owner_table.c.ordinal == 2)
    alterations = (
        delete(owner_table).where(root),
        owner_table.insert().values(expected(version)[0] | {"ordinal": 3}),
        update(owner_table).where(root).values(file_name="e\u0301.sh"),
        update(owner_table).where(root).values(subgraph_name="body"),
        update(owner_table).where(body).values(subgraph_name=None),
        update(owner_table).where(body).values(payload_state="expired"),
        update(owner_table).where(body).values(object_incarnation="other-object"),
        update(owner_table).where(body).values(total_size=len(PAYLOAD) + 1),
    )
    assert statements.insert is not None
    changed_parameters = statements.parameters()
    changed_parameters["file_name_1"] = "substituted.sh"

    with pytest.raises(GraphValueRejected) as wrong_scope:
        prepare_graph_publication_owners(
            attachment, neighbor_target, database.dialect, max_bind_units=OWNER_LIMIT
        )
    with pytest.raises(GraphValueRejected) as short:
        prepare_graph_publication_owners(
            attachment, target, database.dialect, max_bind_units=1
        )
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(statements)
    with pytest.raises(ValueError, match="not iterable") as encoded:
        jsonable_encoder(statements)
    async with database.transaction() as connection:
        with closing(
            await connection.execute(statements.insert, statements.parameters())
        ) as result:
            markers = result.scalars().all()
        matched = await native_owner_match(connection, statements)
        absent = await native_owner_match(connection, missing)
        empty_match = await native_owner_match(connection, empty_statement)
        decoys = await owner_decoy_observations(
            connection, version, missing, empty_statement
        )
        refusals: list[object] = []
        for alteration in alterations:
            nested = await connection.begin_nested()
            try:
                await connection.execute(alteration)
                refusals.append(await native_owner_match(connection, statements))
            finally:
                await nested.rollback()
        await connection.execute(owner_table.insert(), expected(neighbor))
        false_empty = await native_owner_match(connection, empty_statement)
    async with database.read_snapshot() as connection:
        after_commit = await native_owner_match(connection, statements)
        with closing(
            await connection.execute(
                select(owner_table).where(scope).order_by(owner_table.c.ordinal)
            )
        ) as result:
            persisted = [dict(row) for row in result.mappings()]

    assert_refused(wrong_scope.value, GraphValueFailure.INVALID)
    assert_refused(short.value, GraphValueFailure.BUDGET)
    assert statements.parameters() != changed_parameters
    assert empty_statement.insert is None
    assert empty_statement.count == 0
    assert empty_statement.parameters() == {"graph_version_id": neighbor}
    assert markers == [1, 1, 1]
    assert all(type(marker) is int for marker in markers)
    assert type(matched) is int
    assert matched == after_commit == empty_match == 1
    assert persisted == expected(version)
    assert absent is None
    assert false_empty is None
    assert decoys == (3, None, 1)
    assert refusals == [None] * len(alterations)
    assert PRIVATE_OBJECT not in repr(statements)
    assert PRIVATE_OBJECT not in repr(encoded.value)
    assert PRIVATE_OBJECT not in str(statements.insert) + str(statements.compare)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_native_owner_insert_refuses_a_later_conflict_without_a_partial_set(
    database: Database,
    owner_preparations: tuple[
        PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
    ],
) -> None:
    ledger = ConnectionLedger(database)
    prepared, _ = owner_preparations
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph, 1)
        await connection.execute(owner_table.insert(), expected(version)[2])
    target = (namespace, graph, version)
    statements = prepare_graph_publication_owners(
        attached(prepared, target), target, database.dialect, max_bind_units=OWNER_LIMIT
    )
    assert statements.insert is not None
    ordinals = select(owner_table.c.ordinal).where(
        owner_table.c.graph_version_id == version
    )

    async with database.transaction() as connection:
        nested = await connection.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await connection.execute(statements.insert, statements.parameters())
            if database.dialect is Dialect.SQLITE:
                assert (await connection.execute(ordinals)).scalars().all() == [2]
        finally:
            await nested.rollback()
        saved = (await connection.execute(ordinals)).scalars().all()
        verified = await native_owner_match(connection, statements)

    assert saved == [2]
    assert verified is None
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.fixture(scope="module")
def maximum_owner_preparation() -> tuple[
    PreparedGraphDeploymentInputs, tuple[str, ...]
]:
    names = tuple(f"file-{index:02d}.sh" for index in range(MAX_FILES))
    raw = command_document(authored_manifest([resource(name) for name in names]))
    files = tuple(CommandResourceContent(ROOT, name, b"") for name in names)
    return prepare_inputs(canonical(raw), files), names


@pytest.mark.asyncio
async def test_native_maximum_owner_batch_and_empty_probe_use_the_scoped_index(
    database: Database,
    maximum_owner_preparation: tuple[PreparedGraphDeploymentInputs, tuple[str, ...]],
    owner_preparations: tuple[
        PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs
    ],
) -> None:
    ledger = ConnectionLedger(database)
    prepared, names = maximum_owner_preparation
    _, empty = owner_preparations
    sqlite = database.dialect is Dialect.SQLITE
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph, 1)
        empty_version = await seeder.graph_version(graph, 2)
        history = [await seeder.graph_version(graph, number) for number in range(3, 19)]
        await connection.execute(
            owner_table.insert(),
            [
                row | {"ordinal": ordinal, "file_name": name}
                for older in history
                for row in (expected(older)[0],)
                for ordinal, name in enumerate(names)
            ],
        )
        await connection.exec_driver_sql("ANALYZE graph_version_resource_owner")
    scope = GraphResourceScope(namespace, graph, version)
    target = (namespace, graph, version)
    reference = content(store=PRIVATE_STORE, incarnation="empty-object")
    candidate = attach_graph_deployment_references(
        prepared,
        scope,
        tuple(GraphResourceOwner(scope, ROOT, name, reference) for name in names),
        limits=ATTACH_LIMITS,
    )
    statements = prepare_graph_publication_owners(
        candidate, target, database.dialect, max_bind_units=MAX_FILES * OWNER_LIMIT
    )
    empty_scope = GraphResourceScope(namespace, graph, empty_version)
    empty_statement = prepare_graph_publication_owners(
        attach_graph_deployment_references(
            empty, empty_scope, (), limits=ATTACH_LIMITS
        ),
        (namespace, graph, empty_version),
        database.dialect,
        max_bind_units=1,
    )
    assert statements.insert is not None
    prefix = "EXPLAIN QUERY PLAN " if sqlite else "EXPLAIN (COSTS OFF) "

    async with database.transaction() as connection:
        nested = await connection.begin_nested()
        try:
            with executed_queries(connection) as inserted_queries:
                with closing(
                    await connection.execute(statements.insert, statements.parameters())
                ) as result:
                    markers = result.scalars().all()
            with executed_queries(connection) as compared_queries:
                full_match = await native_owner_match(connection, statements)
                empty_match = await native_owner_match(connection, empty_statement)
            plans = [
                str((await connection.exec_driver_sql(prefix + sql, arguments)).all())
                for sql, arguments in compared_queries
            ]
        finally:
            await nested.rollback()
        missing = await native_owner_match(connection, statements)

    assert statements.count == MAX_FILES
    assert len(markers) == MAX_FILES
    assert all(type(marker) is int and marker == 1 for marker in markers)
    assert type(full_match) is int
    assert full_match == empty_match == 1
    assert missing is None
    [(_, arguments)] = inserted_queries
    assert len(arguments) == (10 * MAX_FILES if sqlite else 9 * MAX_FILES + 1)
    assert [len(arguments) for _, arguments in compared_queries] == [
        9 * MAX_FILES + 2,
        1,
    ]
    for plan in plans:
        if sqlite:
            assert "SEARCH a USING " in plan
            assert "(graph_version_id=?)" in plan
        else:
            assert "pk_graph_version_resource_owner" in plan
            assert "Index Cond:" in plan
            assert "graph_version_id" in plan
    assert ledger.outstanding == database.units_in_flight() == 0
