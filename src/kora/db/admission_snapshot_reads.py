from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import (
    Executable,
    Table,
    Text,
    and_,
    case,
    column,
    func,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.admission_discovery_v1 import CANDIDATE_ROWS
from kora.db.claim_sources_v1 import SQLITE_KEY
from kora.db.identifiers import is_identifier
from kora.db.run_input_locator_v1 import SQLITE_RUN_KEY
from kora.db.run_input_parent import SQLITE_MAX_PARENT_ALLOWANCE, sqlite_storage_width
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import node_instance, price_table, run
from kora.db.types import IDENTIFIER_LENGTH, PATH_LENGTH, POSTGRESQL, SQLITE

InitialAdmissionStatus = Literal["pending", "ready", "waiting"]


@dataclass(frozen=True, slots=True, repr=False)
class AdmissionCandidateBinding:
    run: RunInputBinding
    node_id: str
    node_path: str
    status: InitialAdmissionStatus


def _bounded_text(
    dialect: str, field: ColumnElement[str], maximum: int
) -> ColumnElement[str]:
    fits = func.octet_length(field) <= 4 * maximum
    if dialect == SQLITE:
        fits = and_(func.typeof(field) == "text", fits)
        text_fits = and_(
            func.length(field) <= maximum,
            func.instr(field, func.char(0)) == 0,
        )
        return case((fits, case((text_fits, field))))
    return case((fits, field))


def _owner_statement(
    dialect: str,
    owner: Table,
    identity: str,
    allowance: int,
    *,
    agent_only: bool = False,
) -> Executable:
    node = owner is node_instance
    names = (
        (("run_id", IDENTIFIER_LENGTH), ("node_path", PATH_LENGTH), ("status", 7))
        if node
        else tuple(
            (name, IDENTIFIER_LENGTH)
            for name in ("namespace_id", "graph_id", "graph_version_id")
        )
    )
    valid = and_(
        literal_column(CANDIDATE_ROWS)
        if node
        else column("status", Text()) == "running",
        column("deadline_expired_at").is_(None),
    )
    if node and agent_only:
        valid = and_(valid, column("kind", Text()) == "agent")
    key = column("id", Text())
    predicate = key == identity
    if dialect == SQLITE:
        width = sum(
            (sqlite_storage_width(column(field.name)) for field in owner.c),
            literal(9 * (len(owner.c) + 1)),
        )
        valid = case((width <= allowance, valid), else_=False)
        source = (
            text("main.node_instance INDEXED BY ix_claim_node_locator")
            if node
            else text("main.run INDEXED BY ix_run_input_locator")
        )
        predicate = and_(
            key.collate("BINARY") == identity,
            literal_column(SQLITE_KEY if node else SQLITE_RUN_KEY),
        )
    else:
        source = text("ONLY node_instance") if node else text("ONLY run")
    return (
        select(
            *(
                case((valid, _bounded_text(dialect, column(name, Text()), maximum)))
                for name, maximum in names
            )
        )
        .select_from(source)
        .where(predicate)
    )


async def read_admission_candidate(
    connection: AsyncConnection,
    node_id: str,
    *,
    node_row: int,
    run_row: int,
    agent_only: bool = False,
) -> AdmissionCandidateBinding | None:
    transaction = connection.get_transaction()
    dialect = connection.dialect.name
    if (
        type(node_id) is not str
        or not is_identifier(node_id)
        or type(agent_only) is not bool
        or any(
            type(limit) is not int or not 0 < limit <= SQLITE_MAX_PARENT_ALLOWANCE
            for limit in (node_row, run_row)
        )
        or dialect not in (SQLITE, POSTGRESQL)
        or transaction is None
        or not transaction.is_active
    ):
        return None
    with await connection.execute(
        _owner_statement(
            dialect, node_instance, node_id, node_row, agent_only=agent_only
        )
    ) as result:
        node = result.first()
    if node is None:
        return None
    run_id, path, status = node
    if (
        type(run_id) is not str
        or not is_identifier(run_id)
        or type(path) is not str
        or not 0 < len(path) <= PATH_LENGTH
        or "\0" in path
        or status not in ("pending", "ready", "waiting")
    ):
        return None
    with await connection.execute(
        _owner_statement(dialect, run, run_id, run_row)
    ) as result:
        scope = result.first()
    if (
        scope is None
        or not all(type(value) is str and is_identifier(value) for value in scope)
        or connection.get_transaction() is not transaction
        or not transaction.is_active
    ):
        return None
    return AdmissionCandidateBinding(
        RunInputBinding(*cast(tuple[str, str, str], tuple(scope)), run_id),
        node_id,
        path,
        status,
    )


async def read_admission_price_id(
    connection: AsyncConnection, namespace_id: str, at: datetime
) -> str | None:
    dialect = connection.dialect.name
    if (
        type(namespace_id) is not str
        or not is_identifier(namespace_id)
        or at.tzinfo is None
        or at.utcoffset() is None
        or dialect not in (SQLITE, POSTGRESQL)
    ):
        return None
    for scope in (namespace_id, None):
        with await connection.execute(
            select(_bounded_text(dialect, price_table.c.id, IDENTIFIER_LENGTH))
            .where(
                price_table.c.namespace_id == scope,
                text("price_table.version = 1"),
                price_table.c.effective_from <= at,
            )
            .order_by(price_table.c.effective_from.desc())
            .limit(1)
        ) as result:
            row = result.first()
        if row is not None:
            identity: object = row[0]
            return (
                identity if type(identity) is str and is_identifier(identity) else None
            )
    return None
