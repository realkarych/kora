from typing import Literal

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH, transport_limit
from kora.db.identifiers import is_identifier
from kora.db.types import DIGEST_LENGTH

SQLiteEncoding = Literal["utf-8", "utf-16le", "utf-16be"]

SQLITE_PROJECTION = text("""
SELECT CAST(v.ir AS BLOB), CAST(v.ir_digest AS BLOB),
       CAST(v.execution_contract AS BLOB), CAST(v.execution_contract_digest AS BLOB),
       CAST(v.deployment_digest AS BLOB), CAST(v.resources AS BLOB)
FROM main.graph AS g CROSS JOIN main.graph_version AS v
     INDEXED BY ix_graph_version_graph_id_id
WHERE g.id = :graph_id AND g.namespace_id = :namespace_id
  AND v.graph_id = g.id AND v.id = :version_id
  AND CASE
    WHEN typeof(v.value_encoding_version) <> 'integer' THEN 0
    WHEN v.value_encoding_version <> 1 THEN 0
    WHEN typeof(v.ir) <> 'text' OR typeof(v.ir_digest) <> 'text'
      OR typeof(v.execution_contract) <> 'text'
      OR typeof(v.execution_contract_digest) <> 'text'
      OR typeof(v.deployment_digest) <> 'text' OR typeof(v.resources) <> 'text' THEN 0
    ELSE octet_length(v.ir) + octet_length(v.execution_contract) <= :max_pair_bytes
      AND octet_length(v.ir_digest) = :digest_bytes
      AND octet_length(v.execution_contract_digest) = :digest_bytes
      AND octet_length(v.deployment_digest) = :digest_bytes
      AND octet_length(v.resources) = :resource_bytes
  END
""")

POSTGRESQL_PROJECTION = text("""
WITH rendered AS MATERIALIZED (
    SELECT pg_catalog.convert_to(v.ir::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding()) AS ir,
           pg_catalog.convert_to(v.ir_digest::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding()) AS ir_digest,
           pg_catalog.convert_to(v.execution_contract::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding())
               AS execution_contract,
           pg_catalog.convert_to(v.execution_contract_digest::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding())
               AS execution_contract_digest,
           pg_catalog.convert_to(v.deployment_digest::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding()) AS deployment_digest,
           pg_catalog.convert_to(v.resources::pg_catalog.text,
                                 pg_catalog.getdatabaseencoding()) AS resources
    FROM ONLY graph AS g JOIN ONLY graph_version AS v
      ON v.graph_id OPERATOR(pg_catalog.=) g.id
    WHERE g.id OPERATOR(pg_catalog.=) :graph_id
      AND g.namespace_id OPERATOR(pg_catalog.=) :namespace_id
      AND v.id OPERATOR(pg_catalog.=) :version_id
      AND v.value_encoding_version OPERATOR(pg_catalog.=) 1
      AND pg_catalog.jsonb_typeof(v.ir) OPERATOR(pg_catalog.=) 'string'
      AND pg_catalog.jsonb_typeof(v.execution_contract) OPERATOR(pg_catalog.=) 'string'
      AND pg_catalog.jsonb_typeof(v.resources) OPERATOR(pg_catalog.=) 'object'
)
SELECT ir, ir_digest, execution_contract, execution_contract_digest,
       deployment_digest, resources
FROM rendered
WHERE pg_catalog.octet_length(ir)::pg_catalog.int8 OPERATOR(pg_catalog.+)
      pg_catalog.octet_length(execution_contract)::pg_catalog.int8
          OPERATOR(pg_catalog.<=) CAST(:max_pair_bytes AS pg_catalog.int8)
  AND pg_catalog.octet_length(ir_digest) OPERATOR(pg_catalog.=) :digest_bytes
  AND pg_catalog.octet_length(execution_contract_digest)
          OPERATOR(pg_catalog.=) :digest_bytes
  AND pg_catalog.octet_length(deployment_digest) OPERATOR(pg_catalog.=) :digest_bytes
  AND pg_catalog.octet_length(resources)
          OPERATOR(pg_catalog.=) CAST(:resource_bytes AS pg_catalog.int8)
""")


def graph_version_admitted_projection_statement(
    dialect: Dialect,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    *,
    max_encoded_bytes: int,
    resource_transport_bytes: int,
    sqlite_encoding: SQLiteEncoding | None = None,
) -> TextClause:
    if not all(
        type(value) is str and is_identifier(value)
        for value in (namespace_id, graph_id, version_id)
    ):
        del namespace_id, graph_id, version_id
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if any(
        type(limit) is not int or not 0 < limit <= MAX_SQL_LENGTH
        for limit in (max_encoded_bytes, resource_transport_bytes)
    ):
        del max_encoded_bytes, resource_transport_bytes
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if (
        dialect is Dialect.SQLITE
        and type(sqlite_encoding) is str
        and sqlite_encoding in ("utf-8", "utf-16le", "utf-16be")
    ):
        statement = SQLITE_PROJECTION
        width = 1 if sqlite_encoding == "utf-8" else 2
    elif dialect is Dialect.POSTGRESQL and sqlite_encoding is None:
        statement = POSTGRESQL_PROJECTION
        width = 1
    else:
        del dialect, sqlite_encoding
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return statement.bindparams(
        namespace_id=namespace_id,
        graph_id=graph_id,
        version_id=version_id,
        max_pair_bytes=transport_limit(max_encoded_bytes, width),
        digest_bytes=width * DIGEST_LENGTH,
        resource_bytes=resource_transport_bytes,
    )
