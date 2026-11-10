from sqlalchemy import bindparam, text
from sqlalchemy.sql.elements import TextClause

from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.types import UtcTimestamp

SAVED_LOCATOR = "publication_saved_locator"
_TIMESTAMPS = ("deployed_at", "created_at", "updated_at")

_SQLITE = text("""
SELECT CASE WHEN
       typeof(v.id) = 'text' AND CAST(v.id AS BLOB) = CAST(:id AS BLOB)
   AND typeof(v.graph_id) = 'text'
       AND CAST(v.graph_id AS BLOB) = CAST(:graph_id AS BLOB)
   AND typeof(v.number) = 'integer' AND v.number = :number
   AND typeof(v.ir) = 'text'
       AND CAST(v.ir AS BLOB) = CAST(:publication_ir_json AS BLOB)
   AND typeof(v.ir_digest) = 'text'
       AND CAST(v.ir_digest AS BLOB) = CAST(:ir_digest AS BLOB)
   AND typeof(v.source_digest) = 'text'
       AND CAST(v.source_digest AS BLOB) = CAST(:source_digest AS BLOB)
   AND typeof(v.execution_contract) = 'text'
       AND CAST(v.execution_contract AS BLOB)
           = CAST(:publication_execution_json AS BLOB)
   AND typeof(v.execution_contract_digest) = 'text'
       AND CAST(v.execution_contract_digest AS BLOB)
           = CAST(:execution_contract_digest AS BLOB)
   AND typeof(v.deployment_digest) = 'text'
       AND CAST(v.deployment_digest AS BLOB) = CAST(:deployment_digest AS BLOB)
   AND typeof(v.resolved_digests) = 'text'
       AND CAST(v.resolved_digests AS BLOB)
           = CAST(:publication_resolved_digests_json AS BLOB)
   AND typeof(v.resources) = 'text'
       AND CAST(v.resources AS BLOB) = CAST(:publication_resources_json AS BLOB)
   AND typeof(v.deployed_by) = 'text'
       AND CAST(v.deployed_by AS BLOB) = CAST(:publication_deployed_by AS BLOB)
   AND typeof(v.deployed_at) = 'text'
       AND CAST(v.deployed_at AS BLOB) = CAST(:deployed_at AS BLOB)
   AND typeof(v.status) = 'text' AND CAST(v.status AS BLOB) = CAST(:status AS BLOB)
   AND typeof(v.created_at) = 'text'
       AND CAST(v.created_at AS BLOB) = CAST(:created_at AS BLOB)
   AND typeof(v.updated_at) = 'text'
       AND CAST(v.updated_at AS BLOB) = CAST(:updated_at AS BLOB)
   AND typeof(v.value_encoding_version) = 'integer'
       AND v.value_encoding_version = :value_encoding_version
THEN 1 ELSE 0 END AS matches
FROM main.graph_version AS v NOT INDEXED
WHERE v.rowid = :publication_saved_locator
""").bindparams(
    *(bindparam(name, type_=UtcTimestamp(), required=True) for name in _TIMESTAMPS)
)

_POSTGRESQL = text("""
WITH expected AS (
    SELECT CAST(:id AS pg_catalog.text) AS id,
           CAST(:graph_id AS pg_catalog.text) AS graph_id,
           CAST(CAST(:publication_ir_json AS pg_catalog.text) AS pg_catalog.jsonb)
               ::pg_catalog.text AS ir,
           CAST(:ir_digest AS pg_catalog.text) AS ir_digest,
           CAST(:source_digest AS pg_catalog.text) AS source_digest,
           CAST(CAST(:publication_execution_json AS pg_catalog.text)
               AS pg_catalog.jsonb)::pg_catalog.text AS execution_contract,
           CAST(:execution_contract_digest AS pg_catalog.text)
               AS execution_contract_digest,
           CAST(:deployment_digest AS pg_catalog.text) AS deployment_digest,
           CAST(CAST(:publication_resolved_digests_json AS pg_catalog.text)
               AS pg_catalog.jsonb)::pg_catalog.text AS resolved_digests,
           CAST(CAST(:publication_resources_json AS pg_catalog.text)
               AS pg_catalog.jsonb)::pg_catalog.text AS resources,
           CAST(:publication_deployed_by AS pg_catalog.text) AS deployed_by,
           CAST(:status AS pg_catalog.text) AS status,
           pg_catalog.getdatabaseencoding() AS encoding
)
SELECT (
       pg_catalog.convert_to(v.id::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.id, e.encoding)
   AND pg_catalog.convert_to(v.graph_id::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.graph_id, e.encoding)
   AND v.number OPERATOR(pg_catalog.=) CAST(:number AS pg_catalog.int4)
   AND pg_catalog.convert_to(v.ir::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.ir, e.encoding)
   AND pg_catalog.convert_to(v.ir_digest::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.ir_digest, e.encoding)
   AND pg_catalog.convert_to(v.source_digest::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.source_digest, e.encoding)
   AND pg_catalog.convert_to(v.execution_contract::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=)
           pg_catalog.convert_to(e.execution_contract, e.encoding)
   AND pg_catalog.convert_to(v.execution_contract_digest::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=)
           pg_catalog.convert_to(e.execution_contract_digest, e.encoding)
   AND pg_catalog.convert_to(v.deployment_digest::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.deployment_digest, e.encoding)
   AND pg_catalog.convert_to(v.resolved_digests::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.resolved_digests, e.encoding)
   AND pg_catalog.convert_to(v.resources::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.resources, e.encoding)
   AND pg_catalog.convert_to(v.deployed_by::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.deployed_by, e.encoding)
   AND v.deployed_at OPERATOR(pg_catalog.=) CAST(:deployed_at AS pg_catalog.timestamptz)
   AND pg_catalog.convert_to(v.status::pg_catalog.text, e.encoding)
           OPERATOR(pg_catalog.=) pg_catalog.convert_to(e.status, e.encoding)
   AND v.created_at OPERATOR(pg_catalog.=) CAST(:created_at AS pg_catalog.timestamptz)
   AND v.updated_at OPERATOR(pg_catalog.=) CAST(:updated_at AS pg_catalog.timestamptz)
   AND v.value_encoding_version OPERATOR(pg_catalog.=)
           CAST(:value_encoding_version AS pg_catalog.int4)
) IS TRUE AS matches
FROM ONLY graph_version AS v CROSS JOIN expected AS e
WHERE v.ctid OPERATOR(pg_catalog.=)
      CAST(CAST(:publication_saved_locator AS pg_catalog.text) AS pg_catalog.tid)
""")


_SQLITE_INSERT = text("""
INSERT INTO main.graph_version (
    id, graph_id, number, ir, ir_digest, source_digest, execution_contract,
    execution_contract_digest, deployment_digest, resolved_digests, resources,
    deployed_by, deployed_at, status, created_at, updated_at, value_encoding_version
) VALUES (
    :id, :graph_id, :number, :publication_ir_json, :ir_digest, :source_digest,
    :publication_execution_json, :execution_contract_digest, :deployment_digest,
    :publication_resolved_digests_json, :publication_resources_json,
    :publication_deployed_by, :deployed_at, :status, :created_at, :updated_at,
    :value_encoding_version
)
RETURNING rowid
""").bindparams(
    *(bindparam(name, type_=UtcTimestamp(), required=True) for name in _TIMESTAMPS)
)

_POSTGRESQL_INSERT = text("""
INSERT INTO graph_version (
    id, graph_id, number, ir, ir_digest, source_digest, execution_contract,
    execution_contract_digest, deployment_digest, resolved_digests, resources,
    deployed_by, deployed_at, status, created_at, updated_at, value_encoding_version
) VALUES (
    CAST(:id AS pg_catalog.text),
    CAST(:graph_id AS pg_catalog.text),
    CAST(:number AS pg_catalog.int4),
    CAST(CAST(:publication_ir_json AS pg_catalog.text) AS pg_catalog.jsonb),
    CAST(:ir_digest AS pg_catalog.text),
    CAST(:source_digest AS pg_catalog.text),
    CAST(CAST(:publication_execution_json AS pg_catalog.text) AS pg_catalog.jsonb),
    CAST(:execution_contract_digest AS pg_catalog.text),
    CAST(:deployment_digest AS pg_catalog.text),
    CAST(CAST(:publication_resolved_digests_json AS pg_catalog.text)
        AS pg_catalog.jsonb),
    CAST(CAST(:publication_resources_json AS pg_catalog.text) AS pg_catalog.jsonb),
    CAST(:publication_deployed_by AS pg_catalog.text),
    CAST(:deployed_at AS pg_catalog.timestamptz),
    CAST(:status AS pg_catalog.text),
    CAST(:created_at AS pg_catalog.timestamptz),
    CAST(:updated_at AS pg_catalog.timestamptz),
    CAST(:value_encoding_version AS pg_catalog.int4)
)
RETURNING ctid::pg_catalog.text
""")


_SQLITE_ADMISSION_INSERT = text("""
INSERT OR ABORT INTO main.graph_version_read_admission (
    graph_version_id, format_version, db_input_bytes, resource_transport_bytes
) VALUES (:graph_version_id, 1, :db_input_bytes, :resource_transport_bytes)
RETURNING 1
""")

_POSTGRESQL_ADMISSION_INSERT = text("""
INSERT INTO graph_version_read_admission (
    graph_version_id, format_version, db_input_bytes, resource_transport_bytes
) VALUES (
    CAST(:graph_version_id AS pg_catalog.text),
    1::pg_catalog.int4,
    CAST(:db_input_bytes AS pg_catalog.int8),
    CAST(:resource_transport_bytes AS pg_catalog.int8)
)
RETURNING 1::pg_catalog.int4
""")


def graph_publication_admission_statement(dialect: Dialect) -> TextClause:
    if dialect is Dialect.SQLITE:
        return _SQLITE_ADMISSION_INSERT
    if dialect is Dialect.POSTGRESQL:
        return _POSTGRESQL_ADMISSION_INSERT
    del dialect
    raise_safe(GraphValueRejected(GraphValueFailure.INVALID))


def graph_publication_insert_statement(dialect: Dialect) -> TextClause:
    if dialect is Dialect.SQLITE:
        return _SQLITE_INSERT
    if dialect is Dialect.POSTGRESQL:
        return _POSTGRESQL_INSERT
    del dialect
    raise_safe(GraphValueRejected(GraphValueFailure.INVALID))


def graph_publication_saved_statement(dialect: Dialect) -> TextClause:
    if dialect is Dialect.SQLITE:
        return _SQLITE
    if dialect is Dialect.POSTGRESQL:
        return _POSTGRESQL
    del dialect
    raise_safe(GraphValueRejected(GraphValueFailure.INVALID))
