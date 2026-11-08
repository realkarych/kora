import pickle
from hashlib import sha256
from traceback import walk_tb
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    attach_graph_deployment_references,
)
from kora.api.graph_execution_preparation import PreparedGraphDeploymentInputs
from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.identifiers import encode
from kora.db.tables import graph_version_resource_owner as owner_table
from kora.engine.graph_command_manifests import CommandResourceContent
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
