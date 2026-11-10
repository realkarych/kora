from string import Template

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_command_manifests import MAX_FILES

OWNER_FIELDS = (
    "ordinal",
    "subgraph_name",
    "node_name",
    "file_name",
    "store_id",
    "payload_digest",
    "object_incarnation",
    "total_size",
    "payload_state",
)
_INTEGER_FIELDS = frozenset(("ordinal", "total_size"))
_SQLITE_TABLE = "main.graph_version_resource_owner"
_POSTGRESQL_TABLE = "graph_version_resource_owner"
_POSTGRESQL_ENCODING = "pg_catalog.getdatabaseencoding()"
OWNER_VERSION = "graph_version_id"
OWNER_COUNT = "expected_count"
_INSERT = Template("""
$target ($fields) VALUES
$values
RETURNING $marker
""")
_EMPTY_COMPARISON = Template("""
SELECT 1 WHERE NOT EXISTS (
    SELECT 1 FROM $relation AS a WHERE $scope
)
""")
_COMPARISON = Template("""
WITH expected ($fields) AS (VALUES
$values
), selected AS MATERIALIZED (
    SELECT $fields FROM $relation AS a
    WHERE $scope
    ORDER BY a.ordinal LIMIT $row_limit
)
SELECT 1 WHERE
    (SELECT $count_rows FROM selected) $equality $expected_count
    AND NOT EXISTS (
        SELECT 1 FROM expected AS e WHERE NOT EXISTS (
            SELECT 1 FROM selected AS a WHERE $matches
        )
    )
""")


def _bind(name: str, dialect: Dialect, *, integer: bool = False) -> str:
    parameter = ":" + name
    if dialect is Dialect.SQLITE:
        return parameter
    native = "int4" if integer else "text"
    return f"CAST({parameter} AS pg_catalog.{native})"


def _row(index: int, dialect: Dialect) -> str:
    return ", ".join(
        _bind(f"{field}_{index}", dialect, integer=field in _INTEGER_FIELDS)
        for field in OWNER_FIELDS
    )


def _insert(dialect: Dialect, count: int) -> TextClause:
    if dialect is Dialect.SQLITE:
        target = "INSERT OR ABORT INTO " + _SQLITE_TABLE
        marker = "1"
    else:
        target = "INSERT INTO " + _POSTGRESQL_TABLE
        marker = "CAST(1 AS pg_catalog.int4)"
    fields = ", ".join((OWNER_VERSION, *OWNER_FIELDS))
    version = _bind(OWNER_VERSION, dialect)
    values = ",\n".join(
        f"({version}, {_row(index, dialect)})" for index in range(count)
    )
    return text(
        _INSERT.substitute(target=target, fields=fields, values=values, marker=marker)
    )


def _scope(dialect: Dialect) -> tuple[str, str]:
    version = _bind(OWNER_VERSION, dialect)
    if dialect is Dialect.SQLITE:
        return _SQLITE_TABLE, f"a.graph_version_id = {version}"
    return (
        "ONLY " + _POSTGRESQL_TABLE,
        f"a.graph_version_id OPERATOR(pg_catalog.=) {version}",
    )


def _match(field: str, dialect: Dialect) -> str:
    actual, expected = "a." + field, "e." + field
    if dialect is Dialect.SQLITE:
        if field in _INTEGER_FIELDS:
            return f"typeof({actual}) = 'integer' AND {actual} = {expected}"
        matched = (
            f"typeof({actual}) = 'text' AND "
            f"CAST({actual} AS BLOB) = CAST({expected} AS BLOB)"
        )
    else:
        if field in _INTEGER_FIELDS:
            return f"{actual} OPERATOR(pg_catalog.=) {expected}"
        matched = (
            f"pg_catalog.convert_to({actual}, {_POSTGRESQL_ENCODING}) "
            f"OPERATOR(pg_catalog.=) "
            f"pg_catalog.convert_to({expected}, {_POSTGRESQL_ENCODING})"
        )
    if field == "subgraph_name":
        return f"CASE WHEN {expected} IS NULL THEN {actual} IS NULL ELSE {matched} END"
    return matched


def _comparison(dialect: Dialect, count: int) -> TextClause:
    relation, scope = _scope(dialect)
    if count == 0:
        return text(_EMPTY_COMPARISON.substitute(relation=relation, scope=scope))
    fields = ", ".join(OWNER_FIELDS)
    values = ",\n".join(f"({_row(index, dialect)})" for index in range(count))
    equality = "=" if dialect is Dialect.SQLITE else "OPERATOR(pg_catalog.=)"
    count_rows = "count(*)" if dialect is Dialect.SQLITE else "pg_catalog.count(*)"
    expected_count = _bind(OWNER_COUNT, dialect, integer=True)
    matches = " AND\n".join(f"({_match(field, dialect)})" for field in OWNER_FIELDS)
    return text(
        _COMPARISON.substitute(
            fields=fields,
            values=values,
            relation=relation,
            scope=scope,
            row_limit=MAX_FILES + 1,
            count_rows=count_rows,
            equality=equality,
            expected_count=expected_count,
            matches=matches,
        )
    )


def graph_owner_statements(
    dialect: Dialect, count: int
) -> tuple[TextClause | None, TextClause]:
    if (
        type(dialect) is not Dialect
        or type(count) is not int
        or not 0 <= count <= MAX_FILES
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return _insert(dialect, count) if count else None, _comparison(dialect, count)
