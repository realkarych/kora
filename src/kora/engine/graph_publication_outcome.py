from asyncio import CancelledError
from contextlib import closing
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationRejected,
    graph_preparation_parameters,
)
from kora.db.identifiers import is_identifier
from kora.db.transactions import WriterTransactionError, raise_private_metadata_error
from kora.db.types import POSTGRESQL, SQLITE
from kora.engine.locks import LockError, TransitionLocks

_SQLITE_UPDATE = text("""
UPDATE OR ABORT main.graph_deployment_preparation
SET state = :outcome,
    result_graph_id = :result_graph_id,
    result_graph_version_id = :result_graph_version_id
WHERE id = :preparation_id
  AND namespace_id = :namespace_id
  AND graph_name = :graph_name
  AND initial_graph_id IS :initial_graph_id
  AND deployment_digest = :deployment_digest
  AND state = 'open'
  AND result_graph_id IS NULL
  AND result_graph_version_id IS NULL
RETURNING 1
""")

_POSTGRESQL_UPDATE = text("""
UPDATE ONLY graph_deployment_preparation
SET state = CAST(:outcome AS pg_catalog.text),
    result_graph_id = CAST(:result_graph_id AS pg_catalog.text),
    result_graph_version_id = CAST(:result_graph_version_id AS pg_catalog.text)
WHERE id OPERATOR(pg_catalog.=) CAST(:preparation_id AS pg_catalog.text)
  AND namespace_id OPERATOR(pg_catalog.=) CAST(:namespace_id AS pg_catalog.text)
  AND graph_name OPERATOR(pg_catalog.=) CAST(:graph_name AS pg_catalog.text)
  AND (
      (initial_graph_id IS NULL AND CAST(:initial_graph_id AS pg_catalog.text) IS NULL)
      OR initial_graph_id OPERATOR(pg_catalog.=)
         CAST(:initial_graph_id AS pg_catalog.text)
  )
  AND deployment_digest OPERATOR(pg_catalog.=)
      CAST(:deployment_digest AS pg_catalog.text)
  AND state OPERATOR(pg_catalog.=) CAST('open' AS pg_catalog.text)
  AND result_graph_id IS NULL
  AND result_graph_version_id IS NULL
RETURNING 1
""")


class GraphPublicationOutcome(StrEnum):
    PUBLISHED = "published"
    REUSED = "reused"


class GraphPublicationOutcomeRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph publication outcome rejected")


async def _record_publication_outcome(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: GraphPreparationBinding,
    graph_id: str,
    version_id: str,
    outcome: GraphPublicationOutcome,
) -> None:
    if (
        type(locks) is not TransitionLocks
        or type(version_id) is not str
        or not is_identifier(version_id)
        or type(outcome) is not GraphPublicationOutcome
    ):
        raise GraphPublicationOutcomeRejected()
    locks.require_publication_graph_preparation(connection, binding, graph_id)
    dialect = connection.dialect.name
    if dialect == SQLITE:
        statement = _SQLITE_UPDATE
    elif dialect == POSTGRESQL:
        statement = _POSTGRESQL_UPDATE
    else:
        raise GraphPublicationOutcomeRejected()
    parameters = graph_preparation_parameters(binding) | {
        "outcome": outcome.value,
        "result_graph_id": graph_id,
        "result_graph_version_id": version_id,
    }
    with closing(await connection.execute(statement, parameters)) as result:
        marker: object = result.scalar_one_or_none()
    if type(marker) is not int or marker != 1:
        raise GraphPublicationOutcomeRejected()
    locks.require_publication_graph_preparation(connection, binding, graph_id)


async def record_graph_publication_outcome(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: GraphPreparationBinding,
    *,
    graph_id: str,
    version_id: str,
    outcome: GraphPublicationOutcome,
) -> None:
    try:
        await _record_publication_outcome(
            connection, locks, binding, graph_id, version_id, outcome
        )
        return
    except CancelledError as error:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        raise
    except (
        GraphPreparationRejected,
        GraphPublicationOutcomeRejected,
        LockError,
        SQLAlchemyError,
        UnicodeError,
        WriterTransactionError,
    ):
        pass
    finally:
        del connection, locks, binding, graph_id, version_id, outcome
    raise_private_metadata_error(GraphPublicationOutcomeRejected())
