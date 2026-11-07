import re
from dataclasses import dataclass
from typing import Literal, Never

from sqlalchemy import Executable, LargeBinary, and_, case, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import Grouping

from kora.db.graph_preparation_v1 import domain_checks, result_checks
from kora.db.identifiers import IDENTIFIER_LENGTH, is_identifier
from kora.db.tables import graph_deployment_preparation
from kora.db.types import POSTGRESQL, SQLITE

GraphPreparationState = Literal[
    "open", "published", "reused", "closed_without_publication"
]
GRAPH_NAME_PATTERN: re.Pattern[str] = re.compile("[a-z][a-z0-9_]{0,63}")
DIGEST_PATTERN: re.Pattern[str] = re.compile("[0-9a-f]{64}")


class GraphPreparationRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph preparation settlement rejected")


class GraphPreparationReadFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph preparation settlement read failed")


def raise_private_read_error(error: RuntimeError) -> Never:
    try:
        raise error from None
    finally:
        error.__cause__ = None
        error.__context__ = None


@dataclass(frozen=True, repr=False)
class GraphPreparationBinding:
    preparation_id: str
    namespace_id: str
    graph_name: str
    initial_graph_id: str | None
    deployment_digest: str


@dataclass(frozen=True, repr=False)
class GraphPreparationSettlement:
    state: GraphPreparationState
    result_graph_id: str | None
    result_graph_version_id: str | None


def graph_preparation_settlement_statement(
    dialect: str, binding: GraphPreparationBinding
) -> Executable:
    if (
        dialect not in (SQLITE, POSTGRESQL)
        or type(binding) is not GraphPreparationBinding
    ):
        raise_private_read_error(GraphPreparationRejected())
    identifiers = (binding.preparation_id, binding.namespace_id)
    if binding.initial_graph_id is not None:
        identifiers = (*identifiers, binding.initial_graph_id)
    if (
        any(type(value) is not str or not is_identifier(value) for value in identifiers)
        or type(binding.graph_name) is not str
        or GRAPH_NAME_PATTERN.fullmatch(binding.graph_name) is None
        or type(binding.deployment_digest) is not str
        or DIGEST_PATTERN.fullmatch(binding.deployment_digest) is None
    ):
        raise_private_read_error(GraphPreparationRejected())

    fields = graph_deployment_preparation.c
    selected = (fields.state, fields.result_graph_id, fields.result_graph_version_id)
    valid = and_(
        *(
            Grouping[bool](check.sqltext)
            for check in (*domain_checks(dialect), *result_checks())
        )
    )
    if dialect == SQLITE:
        valid = and_(
            valid,
            *(
                field.is_(None)
                | (func.length(sql_cast(field, LargeBinary)) <= 2 * width)
                for field, width in zip(
                    selected,
                    (
                        len("closed_without_publication"),
                        IDENTIFIER_LENGTH,
                        IDENTIFIER_LENGTH,
                    ),
                    strict=True,
                )
            ),
        )
    return (
        select(*(case((valid, field), else_=None) for field in selected))
        .select_from(graph_deployment_preparation)
        .where(
            fields.id == binding.preparation_id,
            fields.namespace_id == binding.namespace_id,
            fields.graph_name == binding.graph_name,
            fields.initial_graph_id.is_not_distinct_from(binding.initial_graph_id),
            fields.deployment_digest == binding.deployment_digest,
        )
    )


async def read_graph_preparation_settlement(
    connection: AsyncConnection, binding: GraphPreparationBinding
) -> GraphPreparationSettlement | None:
    statement = graph_preparation_settlement_statement(connection.dialect.name, binding)
    try:
        result = await connection.execute(statement)
    except StatementError:
        result = None
    if result is None:
        raise_private_read_error(GraphPreparationReadFailed())
    row = result.first()
    if row is None:
        return None

    state: object = row[0]
    graph_id: object = row[1]
    version_id: object = row[2]
    if type(state) is not str:
        raise_private_read_error(GraphPreparationRejected())
    if state == "open" or state == "closed_without_publication":
        if graph_id is None and version_id is None:
            return GraphPreparationSettlement(state, None, None)
    if state == "published" or state == "reused":
        if (
            type(graph_id) is str
            and is_identifier(graph_id)
            and type(version_id) is str
            and is_identifier(version_id)
            and (
                binding.initial_graph_id is None or graph_id == binding.initial_graph_id
            )
        ):
            return GraphPreparationSettlement(state, graph_id, version_id)
    raise_private_read_error(GraphPreparationRejected())
