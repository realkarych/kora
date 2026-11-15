import json
from typing import cast

import pytest
from sqlalchemy import Column, Integer, LargeBinary, MetaData, Table, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.common import context_for
from kora.db.connection import Database
from kora.engine.artifact_projection import (
    check_artifact_projection,
    prepare_artifact_projection,
)
from kora.ir.json import JsonValue
from tests.engine.artifact_projection_cases import (
    LIMITS,
    PATH,
    metadata,
    projection_capture,
    projection_graph,
)
from tests.protocols.effective_artifact_cases import installed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        9007199254740993,
        0.0,
        -0.0,
        5e-324,
        1.7976931348623157e308,
        "nul\0😀",
        [],
        {},
        {"\0😀": [1, 1.0, -0.0]},
        pytest.param("x" * 4094, id="maximum-record"),
    ],
)
async def test_cel_projection_number_kinds_round_trip(
    database: Database, value: JsonValue
) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, value, installed(), context_for(capture), limits=LIMITS
        )
    storage = Table(
        "projection_transport_probe",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("content", LargeBinary, nullable=False),
    )

    async def create(connection: AsyncConnection) -> None:
        await connection.run_sync(storage.create)

    async def persist(connection: AsyncConnection) -> None:
        await connection.execute(
            insert(storage).values(id=1, content=prepared.encoded())
        )

    await database.run(create)

    await database.run(persist)
    async with database.read_transaction() as connection:
        read: object = (
            await connection.execute(select(storage.c.content).where(storage.c.id == 1))
        ).scalar_one()
    restored = check_artifact_projection(
        checked, PATH, read, metadata(prepared), limits=LIMITS
    )

    assert type(read) is bytes
    assert read == prepared.encoded() == restored.encoded()
    decoded = cast(dict[str, JsonValue], json.loads(restored.encoded()))
    assert type(decoded["original"]) is type(value)
    assert restored.identity == prepared.identity
