from sqlalchemy import text

from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate
from kora.db.types import IDENTIFIER_LENGTH, SQLITE

POSTGRES_QUEUE_INDEX = "ix_run_queue_active"
SQLITE_QUEUE_INDEX = "ix_run_queue_active_sqlite"
QUEUE_COLUMNS = ("namespace_id", "graph_id", "status")
ACTIVE_QUEUE_ROWS = "status IN ('pending', 'running')"


def _sqlite_text_width(column: str) -> str:
    return (
        f"typeof({column}) = 'text' AND length(CAST({column} AS BLOB)) "
        f"IN ({IDENTIFIER_LENGTH}, {2 * IDENTIFIER_LENGTH})"
    )


SQLITE_QUEUE_ROWS = (
    f"{_sqlite_text_width('namespace_id')} AND "
    f"{_sqlite_text_width('graph_id')} AND {ACTIVE_QUEUE_ROWS}"
)
_SQLITE_ULID = ascii_predicate(
    SQLITE, "id", ULID_ALPHABET, f"= {IDENTIFIER_LENGTH}", "0-7"
)
SQLITE_QUEUE_KEY = (
    f"CASE WHEN status = 'pending' AND "
    f"(CASE WHEN {_sqlite_text_width('id')} THEN ({_SQLITE_ULID}) ELSE 0 END) IS 1 "
    f"THEN id ELSE '' END"
)
POSTGRES_QUEUE_PATTERN = f"^[0-7][{ULID_ALPHABET}]{{{IDENTIFIER_LENGTH - 1}}}$"
POSTGRES_QUEUE_KEY = (
    "CASE\n"
    "    WHEN status::text = 'pending'::text "
    f"AND octet_length(id::text) = {IDENTIFIER_LENGTH} "
    f"AND id::text ~ '{POSTGRES_QUEUE_PATTERN}'::text THEN id\n"
    "    ELSE ''::character varying\n"
    "END"
)

POSTGRES_QUEUE_INDEX_STATE = text(
    "SELECT pg_catalog.quote_ident(n.nspname), pg_catalog.pg_get_indexdef(c.oid), "
    "i.indisvalid, i.indisready, i.indislive, "
    "i.indrelid = pg_catalog.to_regclass('run') "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid "
    "WHERE n.nspname = pg_catalog.current_schema() AND c.relname = :name"
)

SQLITE_QUEUE_INDEX_STATE = text(
    "SELECT EXISTS(SELECT 1 FROM main.sqlite_schema "
    "WHERE type = 'index' AND tbl_name = 'run' AND name = :name "
    "AND sql = :definition)"
)
SQLITE_QUEUE_INDEX_DEFINITION = (
    f"CREATE INDEX {SQLITE_QUEUE_INDEX} ON run "
    f"({', '.join(QUEUE_COLUMNS)}, ({SQLITE_QUEUE_KEY})) WHERE {SQLITE_QUEUE_ROWS}"
)


def postgres_queue_index_definition(schema: str) -> str:
    return (
        f"CREATE INDEX {POSTGRES_QUEUE_INDEX} ON {schema}.run USING btree "
        f"({', '.join(QUEUE_COLUMNS)}, (\n"
        "CASE\n"
        "    WHEN (((status)::text = 'pending'::text) "
        f"AND (octet_length((id)::text) = {IDENTIFIER_LENGTH}) "
        f"AND ((id)::text ~ '{POSTGRES_QUEUE_PATTERN}'::text)) THEN id\n"
        "    ELSE ''::character varying\n"
        "END)) WHERE ((status)::text = ANY "
        "((ARRAY['pending'::character varying, 'running'::character varying])::text[]))"
    )
