import json
import math
import pickle
from dataclasses import replace
from traceback import walk_tb
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Text, bindparam, func, select

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    attach_graph_deployment_references,
)
from kora.api.graph_execution_preparation import PreparedExecutionContract
from kora.api.graph_publication_values import (
    GraphPublicationBindLimits,
    graph_publication_bind_expressions,
    prepare_graph_publication_binds,
)
from kora.db.connection import Database, Dialect
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_version_identity import (
    GraphVersionPin,
    read_graph_version_identity,
)
from tests.api.test_graph_deployment_attachment import (
    ATTACH_LIMITS,
    attachment_inputs,
    prepare_inputs,
)
from tests.api.test_graph_execution_preparation import READ_LIMITS, deployment_source
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_graph_resource_references import SCOPE, assert_refused

PRIVATE_WIRE = "private-wire-572"
SQL_LOOKALIKE = PRIVATE_WIRE + " '); SELECT graph_version; \"\\"
ENCODED_TEXT = "\x00😀"


def checked_attachment(*, with_files: bool) -> AttachedGraphDeployment:
    ir_text, files, owners = (
        attachment_inputs(SCOPE) if with_files else (deployment_source(()), (), ())
    )
    raw = ObjectReader(ir_text).object()
    raw["x-bind"] = SQL_LOOKALIKE if with_files else PRIVATE_WIRE + "-empty"
    raw["x-text"] = ENCODED_TEXT
    ir_text = render_object(raw, TextBudget(READ_LIMITS.identity.max_encoded_bytes))
    prepared = prepare_inputs(ir_text, files)
    return attach_graph_deployment_references(
        prepared, SCOPE, owners, limits=ATTACH_LIMITS
    )


@pytest.fixture(scope="module")
def publication_candidates() -> tuple[AttachedGraphDeployment, AttachedGraphDeployment]:
    return checked_attachment(with_files=True), checked_attachment(with_files=False)


def publication_limits(
    candidate: AttachedGraphDeployment,
) -> GraphPublicationBindLimits:
    inner = len(candidate.canonical_ir) + len(candidate.canonical_contract)
    resource = len(candidate.resources.text)
    outer = 2 * inner + 4
    exact = resource + sum(
        len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        for value in (candidate.canonical_ir, candidate.canonical_contract)
    )
    assert exact < outer + resource
    return GraphPublicationBindLimits(
        exact,
        inner + resource + outer + 32,
        8 * inner + 2 * resource + 2 * outer + 32,
    )


def assert_private_bind_refusal(
    error: GraphValueRejected, expected: GraphValueFailure
) -> None:
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
    }

    assert_refused(error, expected)
    assert set(frames["prepare_graph_publication_binds"]) == {"reason"}
    assert "_prepare_binds" not in frames
    assert PRIVATE_WIRE not in repr(error)


def test_publication_bind_values_admit_exact_bytes_and_protect_private_state(
    publication_candidates: tuple[AttachedGraphDeployment, AttachedGraphDeployment],
) -> None:
    candidate = publication_candidates[0]
    exact = publication_limits(candidate)
    generic = PreparedExecutionContract(
        candidate.scope,
        candidate.canonical_ir,
        candidate.canonical_contract,
        candidate.digests,
        candidate.resources,
        candidate.owners,
    )
    limits = (
        replace(exact, max_bind_bytes=exact.max_bind_bytes - 1),
        replace(exact, max_retained_bytes=exact.max_retained_bytes - 1),
        replace(exact, max_work=exact.max_work - 1),
        replace(exact, max_work=cast(int, cast(object, PRIVATE_WIRE))),
    )

    prepared = prepare_graph_publication_binds(candidate, limits=exact)
    for short in limits:
        with pytest.raises(GraphValueRejected) as refused:
            prepare_graph_publication_binds(candidate, limits=short)
        assert_private_bind_refusal(refused.value, GraphValueFailure.BUDGET)
    with pytest.raises(GraphValueRejected) as wrong_producer:
        prepare_graph_publication_binds(
            cast(AttachedGraphDeployment, generic), limits=exact
        )
    with pytest.raises(AttributeError, match="immutable") as assignment:
        prepared.ir_json = PRIVATE_WIRE
    with pytest.raises(AttributeError, match="immutable") as deletion:
        delattr(prepared, PRIVATE_WIRE)
    with pytest.raises(TypeError, match="explicit access") as serialization:
        pickle.dumps(prepared)
    with pytest.raises(ValueError, match="not iterable") as encoded:
        jsonable_encoder(prepared)
    changed = prepared.parameters()
    changed["publication_ir_json"] = PRIVATE_WIRE

    assert_private_bind_refusal(wrong_producer.value, GraphValueFailure.INVALID)
    assert (
        len(prepared.ir_json)
        + len(prepared.execution_json)
        + len(prepared.resources_json)
        == exact.max_bind_bytes
    )
    assert json.loads(prepared.ir_json) == candidate.canonical_ir
    assert json.loads(prepared.execution_json) == candidate.canonical_contract
    assert prepared.resources_json is candidate.resources.text
    assert prepared.digests is candidate.digests
    assert prepared.parameters()["publication_ir_json"] is prepared.ir_json
    assert PRIVATE_WIRE not in repr(prepared)
    assert PRIVATE_WIRE not in repr(encoded.value)
    for error, operation in (
        (assignment.value, "__setattr__"),
        (deletion.value, "__delattr__"),
        (serialization.value, "__getstate__"),
    ):
        frames = [
            frame.f_locals
            for frame, _ in walk_tb(error.__traceback__)
            if frame.f_code.co_name == operation
        ]
        assert frames == [{}]


@pytest.mark.asyncio
async def test_publication_uses_reusable_value_less_native_json_binds(
    database: Database,
    publication_candidates: tuple[AttachedGraphDeployment, AttachedGraphDeployment],
) -> None:
    ledger = ConnectionLedger(database)
    bound = tuple(
        prepare_graph_publication_binds(candidate, limits=publication_limits(candidate))
        for candidate in publication_candidates
    )
    resources = tuple(ObjectReader(item.resources_json).object() for item in bound)
    fields = graph_version.c
    types = (
        func.jsonb_typeof if database.dialect is Dialect.POSTGRESQL else func.json_type
    )
    target = (
        graph_version.update()
        .where(
            fields.id
            == bindparam("publication_version_id", type_=Text(), required=True)
        )
        .values(graph_publication_bind_expressions(database.dialect))
    )

    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        selected = tuple(
            [await seeder.graph_version(graph_id, number) for number in (1, 2)]
        )
        ordinary = tuple(
            [await seeder.graph_version(graph_id, number) for number in (3, 4)]
        )
        for candidate, binds, native_resources, version, baseline in zip(
            publication_candidates, bound, resources, selected, ordinary, strict=True
        ):
            hashes = binds.digests
            identity = {
                "value_encoding_version": 1,
                "ir_digest": hashes.ir_digest,
                "execution_contract_digest": hashes.execution_contract_digest,
                "deployment_digest": hashes.deployment_digest,
            }
            await writer.execute(
                graph_version.update().where(fields.id == version).values(**identity)
            )
            await writer.execute(
                graph_version.update()
                .where(fields.id == baseline)
                .values(
                    **identity,
                    ir=candidate.canonical_ir,
                    execution_contract=candidate.canonical_contract,
                    resources=native_resources,
                )
            )
        template = target.compile(dialect=writer.dialect)
        defaults = template.params
        with executed_queries(writer) as executed:
            for version, binds in zip(selected, bound, strict=True):
                await writer.execute(
                    target, binds.parameters() | {"publication_version_id": version}
                )
    async with database.read_snapshot() as reader:
        saved = [
            await graph_version_by_number(reader, namespace_id, graph_id, number)
            for number in (1, 2, 3, 4)
        ]
        native_types = (
            await reader.execute(
                select(
                    types(fields.ir),
                    types(fields.execution_contract),
                    types(fields.resources),
                )
                .where(fields.id.in_(selected))
                .order_by(fields.number)
            )
        ).all()
        if database.dialect is Dialect.SQLITE:
            sqlite_storage_types = (
                await reader.execute(
                    select(
                        func.typeof(fields.ir),
                        func.typeof(fields.execution_contract),
                        func.typeof(fields.resources),
                    )
                    .where(fields.id.in_(selected))
                    .order_by(fields.number)
                )
            ).all()
        else:
            sqlite_storage_types = []

    assert database.units_in_flight() == ledger.outstanding == 0
    assert len(executed) == 2
    assert executed[0][0] == executed[1][0]
    assert defaults == {
        "publication_ir_json": None,
        "publication_execution_json": None,
        "publication_resources_json": None,
        "publication_version_id": None,
    }
    assert PRIVATE_WIRE not in str(template)
    for (statement, params), binds in zip(executed, bound, strict=True):
        assert PRIVATE_WIRE not in statement
        assert all(
            any(value is bound_text for value in params)
            for bound_text in binds.parameters().values()
        )
    assert bound[0].ir_json != bound[1].ir_json
    assert bound[0].resources_json != bound[1].resources_json
    expected_types = (
        ("string", "string", "object")
        if database.dialect is Dialect.POSTGRESQL
        else ("text", "text", "object")
    )
    assert native_types == [expected_types] * 2
    if database.dialect is Dialect.SQLITE:
        assert sqlite_storage_types == [("text", "text", "text")] * 2
    for number, (candidate, binds, version, native_resources) in enumerate(
        zip(publication_candidates, bound, selected, resources, strict=True)
    ):
        current, baseline = saved[number], saved[number + 2]
        assert current is not None
        assert baseline is not None
        assert binds.digests is candidate.digests
        assert current.resources == baseline.resources == native_resources
        assert current.ir == baseline.ir == candidate.canonical_ir
        assert (
            current.execution_contract
            == baseline.execution_contract
            == candidate.canonical_contract
        )
        checked = read_graph_version_identity(
            current,
            GraphVersionPin(
                graph_id, version, candidate.digests.execution_contract_digest
            ),
            limits=READ_LIMITS.identity,
        )
        assert checked.canonical_ir == candidate.canonical_ir
        assert checked.canonical_contract == candidate.canonical_contract
        assert checked.values.ir["x-text"] == ENCODED_TEXT
        assert math.copysign(1, cast(float, checked.values.ir["x-signed-zero"])) == -1
        assert type(checked.values.ir["x-signed-zero"]) is float
        assert type(checked.values.ir["x-decimal"]) is int
        assert checked.values.ir["x-decimal"].bit_length() > 2_000
