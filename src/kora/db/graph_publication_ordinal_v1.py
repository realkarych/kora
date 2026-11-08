from kora.db.types import IDENTIFIER_LENGTH

ORDINAL_VALID_INDEX: str = "ix_graph_version_ordinal_valid"
ORDINAL_INVALID_INDEX: str = "ix_graph_version_ordinal_invalid"
ORDINAL_VALID_COLUMNS: tuple[str, ...] = ("graph_id", "number")
ORDINAL_INVALID_COLUMNS: tuple[str, ...] = ("graph_id",)
GRAPH_KEY: str = (
    "typeof(graph_id) = 'text' AND "
    f"length(CAST(graph_id AS BLOB)) IN ({IDENTIFIER_LENGTH}, {2 * IDENTIFIER_LENGTH})"
)
POSITIVE_INTEGER: str = "typeof(number) = 'integer' AND number > 0"
SQLITE_ORDINAL_VALID: str = f"{GRAPH_KEY} AND ({POSITIVE_INTEGER}) IS 1"
SQLITE_ORDINAL_INVALID: str = f"{GRAPH_KEY} AND ({POSITIVE_INTEGER}) IS NOT 1"
