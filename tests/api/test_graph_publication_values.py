import asyncio
import json
import math
import pickle
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from traceback import walk_tb
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Text, bindparam, func, select, text

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    attach_graph_deployment_references,
)
from kora.api.graph_execution_preparation import (
    PreparedExecutionContract,
    PreparedGraphDeploymentInputs,
    PreparedLogicalExecutionContract,
)
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
from kora.db.identifiers import encode
from kora.db.migrations import upgrade_database
from kora.db.reads import graph_version_by_number
from kora.db.sqlite_graph_version_bounds import FIXED_ASCII_CHARACTERS
from kora.db.tables import graph_version
from kora.engine.graph_resource_references import GraphResourceScope
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
from tests.support.databases import encoded_sqlite_settings

PRIVATE_WIRE = "private-wire-572"
SQL_LOOKALIKE = PRIVATE_WIRE + " '); SELECT graph_version; \"\\"
ENCODED_TEXT = "\x00😀"


def checked_attachment(
    *, with_files: bool, scope: GraphResourceScope = SCOPE
) -> AttachedGraphDeployment:
    ir_text, files, owners = (
        attachment_inputs(scope) if with_files else (deployment_source(()), (), ())
    )
    raw = ObjectReader(ir_text).object()
    raw["x-bind"] = SQL_LOOKALIKE if with_files else PRIVATE_WIRE + "-empty"
    raw["x-text"] = ENCODED_TEXT
    ir_text = render_object(raw, TextBudget(READ_LIMITS.identity.max_encoded_bytes))
    prepared = prepare_inputs(ir_text, files)
    return attach_graph_deployment_references(
        prepared, scope, owners, limits=ATTACH_LIMITS
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
    retained_scope = 4 * sum(map(len, candidate.scope_ids))
    return GraphPublicationBindLimits(
        exact,
        inner + resource + outer + 32 + retained_scope,
        8 * inner + 2 * resource + 2 * outer + 32 + retained_scope,
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


FIXED_STAMP = datetime(2026, 11, 10, 12, 34, 56, 123456, timezone(timedelta(hours=3)))
SOURCE_DIGEST = "a" * 64
SQLITE_FIXED_STORAGE = text(
    "SELECT typeof(number), typeof(value_encoding_version), deployed_at, created_at, "
    "updated_at, length(CAST(id || graph_id || ir_digest || source_digest || "
    "execution_contract_digest || deployment_digest || deployed_at || created_at || "
    "updated_at || status AS BLOB)) FROM main.graph_version WHERE id = :version"
)


async def assert_fixed_publication_insert(
    database: Database, sqlite_width: int
) -> None:
    ledger = ConnectionLedger(database)
    dialect = database.dialect
    sqlite = dialect is Dialect.SQLITE
    maximum = (1 << (63 if sqlite else 31)) - 1
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        scope = GraphResourceScope(
            namespace_id, await seeder.graph(namespace_id), seeder.new_id()
        )
    candidate = await asyncio.to_thread(
        checked_attachment, with_files=False, scope=scope
    )
    payloads = prepare_graph_publication_binds(
        candidate, limits=publication_limits(candidate)
    )
    fixed = payloads.fixed_values(
        dialect, number=maximum, source_digest=SOURCE_DIGEST, at=FIXED_STAMP
    )
    fields = graph_version.c
    insert = (
        graph_version.insert()
        .values(graph_publication_bind_expressions(dialect))
        .values(fixed)
        .values(resolved_digests={}, deployed_by="publication-owner")
    )
    projection = select(
        fields.id,
        fields.graph_id,
        fields.number,
        fields.ir_digest,
        fields.source_digest,
        fields.execution_contract_digest,
        fields.deployment_digest,
        fields.deployed_at,
        fields.created_at,
        fields.updated_at,
        fields.status,
        fields.value_encoding_version,
        fields.ir,
        fields.execution_contract,
        fields.resources,
    ).where(fields.id == scope.version_id)
    native_sqlite: tuple[object, ...] | None = None

    async with database.transaction() as writer:
        await writer.execute(insert, payloads.parameters())
        observed: tuple[object, ...] = tuple((await writer.execute(projection)).one())
        if sqlite:
            native_sqlite = tuple(
                (
                    await writer.execute(
                        SQLITE_FIXED_STORAGE, {"version": scope.version_id}
                    )
                ).one()
            )
        await writer.rollback()
    async with database.read_transaction() as reader:
        remaining = (await reader.execute(projection)).one_or_none()

    stamp = FIXED_STAMP.astimezone(UTC)
    assert observed == (
        scope.version_id,
        scope.graph_id,
        maximum,
        candidate.digests.ir_digest,
        SOURCE_DIGEST,
        candidate.digests.execution_contract_digest,
        candidate.digests.deployment_digest,
        stamp,
        stamp,
        stamp,
        "active",
        1,
        candidate.canonical_ir,
        candidate.canonical_contract,
        ObjectReader(candidate.resources.text).object(),
    )
    if sqlite:
        raw = "2026-11-10T09:34:56.123456+00:00"
        assert native_sqlite == (
            "integer",
            "integer",
            raw,
            raw,
            raw,
            410 * sqlite_width,
        )
        assert 410 <= FIXED_ASCII_CHARACTERS
    assert remaining is None
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_fixed_publication_binds_own_the_inserted_native_row(
    database: Database,
) -> None:
    await assert_fixed_publication_insert(database, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_fixed_publication_fields_keep_native_utf16_width(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "fixed-width.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        await assert_fixed_publication_insert(database, 2)
    finally:
        await database.close()


def test_fixed_publication_refuses_unrepresentable_values_without_private_state() -> (
    None
):
    scope = GraphResourceScope(encode(1), encode(2), encode(3))
    candidate = checked_attachment(with_files=False, scope=scope)
    non_native_scope = checked_attachment(with_files=False)
    bad_identity = PreparedLogicalExecutionContract(
        candidate.canonical_ir,
        candidate.canonical_contract,
        replace(candidate.digests, execution_contract_digest=PRIVATE_WIRE),
    )
    bad_hash = attach_graph_deployment_references(
        PreparedGraphDeploymentInputs(bad_identity, ()), scope, (), limits=ATTACH_LIMITS
    )
    payloads, invalid_scope, invalid_hash = (
        prepare_graph_publication_binds(item, limits=publication_limits(item))
        for item in (candidate, non_native_scope, bad_hash)
    )
    bad_utc = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    invalid = (
        (payloads, Dialect.SQLITE, 1 << 63, SOURCE_DIGEST, FIXED_STAMP),
        (payloads, Dialect.POSTGRESQL, 1 << 31, SOURCE_DIGEST, FIXED_STAMP),
        (payloads, Dialect.SQLITE, 0, SOURCE_DIGEST, FIXED_STAMP),
        (payloads, Dialect.SQLITE, True, SOURCE_DIGEST, FIXED_STAMP),
        (payloads, Dialect.SQLITE, 1, PRIVATE_WIRE, FIXED_STAMP),
        (payloads, Dialect.SQLITE, 1, "A" * 64, FIXED_STAMP),
        (
            payloads,
            Dialect.POSTGRESQL,
            1,
            SOURCE_DIGEST,
            FIXED_STAMP.replace(tzinfo=None),
        ),
        (payloads, Dialect.SQLITE, 1, SOURCE_DIGEST, FIXED_STAMP.replace(year=999)),
        (payloads, Dialect.SQLITE, 1, SOURCE_DIGEST, bad_utc),
        (invalid_scope, Dialect.SQLITE, 1, SOURCE_DIGEST, FIXED_STAMP),
        (invalid_hash, Dialect.POSTGRESQL, 1, SOURCE_DIGEST, FIXED_STAMP),
    )
    rejected: list[GraphValueRejected] = []

    for prepared, dialect, number, digest, at in invalid:
        with pytest.raises(GraphValueRejected) as refusal:
            prepared.fixed_values(dialect, number=number, source_digest=digest, at=at)
        rejected.append(refusal.value)

    for error in rejected:
        assert_refused(error, GraphValueFailure.INVALID)
        frames = [
            frame.f_locals
            for frame, _ in walk_tb(error.__traceback__)
            if frame.f_code.co_name == "fixed_values"
        ]
        assert frames == [{}]
        assert PRIVATE_WIRE not in repr(error)


def test_publication_retains_the_issued_scope_when_visible_scope_changes() -> None:
    original = GraphResourceScope(encode(1), encode(2), encode(3))
    candidate = checked_attachment(with_files=False, scope=original)
    replacement = [encode(4), encode(5), encode(6)]
    limits = publication_limits(candidate)

    candidate.scope.__init__(*replacement)
    with pytest.raises(GraphValueRejected) as refusal:
        prepare_graph_publication_binds(candidate, limits=limits)
    candidate.scope.__init__(
        original.namespace_id, original.graph_id, original.version_id
    )
    prepared = prepare_graph_publication_binds(candidate, limits=limits)
    candidate.scope.__init__(*replacement)
    fixed = prepared.fixed_values(
        Dialect.SQLITE, number=1, source_digest=SOURCE_DIGEST, at=FIXED_STAMP
    )

    assert_private_bind_refusal(refusal.value, GraphValueFailure.INVALID)
    assert candidate.scope.graph_id == replacement[1]
    assert fixed["id"] == original.version_id
    assert fixed["graph_id"] == original.graph_id


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
