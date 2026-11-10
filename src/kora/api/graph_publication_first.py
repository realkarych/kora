from datetime import datetime

from sqlalchemy import Text, bindparam, text
from sqlalchemy.sql.elements import TextClause

from kora.api.graph_deployment_attachment import is_checked_graph_attachment
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationRejected,
    require_graph_preparation_binding,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.identifiers import is_identifier
from kora.db.types import UtcTimestamp, normalise_timestamp


def _bind_insert(statement: TextClause) -> TextClause:
    return statement.bindparams(
        *(
            bindparam(name, type_=Text(), required=True)
            for name in ("id", "namespace_id", "name")
        ),
        *(
            bindparam(name, type_=UtcTimestamp(), required=True)
            for name in ("created_at", "updated_at")
        ),
    )


_SQLITE_INSERT = _bind_insert(
    text("""
INSERT OR ABORT INTO main.graph (id, namespace_id, name, created_at, updated_at)
VALUES (:id, :namespace_id, :name, :created_at, :updated_at)
RETURNING 1
""")
)
_POSTGRESQL_INSERT = _bind_insert(
    text("""
INSERT INTO graph (id, namespace_id, name, created_at, updated_at)
VALUES (
    CAST(:id AS pg_catalog.text),
    CAST(:namespace_id AS pg_catalog.text),
    CAST(:name AS pg_catalog.text),
    CAST(:created_at AS pg_catalog.timestamptz),
    CAST(:updated_at AS pg_catalog.timestamptz)
)
RETURNING 1
""")
)


def _checked_first_graph_values(
    attachment: object, binding: GraphPreparationBinding, at: datetime
) -> dict[str, str | datetime]:
    require_graph_preparation_binding(binding)
    if (
        not is_checked_graph_attachment(attachment)
        or binding.initial_graph_id is not None
        or type(at) is not datetime
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    namespace_id, graph_id, version_id = attachment.scope_ids
    deployment_digest = attachment.digests.deployment_digest
    name = binding.graph_name
    if (
        not all(
            type(value) is str and is_identifier(value)
            for value in (namespace_id, graph_id, version_id)
        )
        or namespace_id != binding.namespace_id
        or type(deployment_digest) is not str
        or deployment_digest != binding.deployment_digest
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    try:
        stamp = normalise_timestamp(at)
    except (TypeError, ValueError, OverflowError):
        raise GraphValueRejected(GraphValueFailure.INVALID) from None
    if stamp.year < 1000:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return {
        "id": graph_id,
        "namespace_id": namespace_id,
        "name": name,
        "created_at": stamp,
        "updated_at": stamp,
    }


def prepare_first_graph_values(
    attachment: object, binding: GraphPreparationBinding, *, at: datetime
) -> dict[str, str | datetime]:
    try:
        return _checked_first_graph_values(attachment, binding, at)
    except (GraphPreparationRejected, GraphValueRejected):
        pass
    finally:
        del attachment, binding, at
    raise_safe(GraphValueRejected(GraphValueFailure.INVALID))


def first_graph_insert_statement(dialect: Dialect) -> TextClause:
    if dialect is Dialect.SQLITE:
        return _SQLITE_INSERT
    if dialect is Dialect.POSTGRESQL:
        return _POSTGRESQL_INSERT
    del dialect
    raise_safe(GraphValueRejected(GraphValueFailure.INVALID))
