from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate
from kora.db.types import DIGEST_LENGTH, IDENTIFIER_LENGTH, SQLITE

DIGEST_INDEX: str = "ix_graph_version_deployment_digest"
INVALID_ID_INDEX: str = "ix_graph_version_deployment_digest_invalid_id"
LOCATOR_COLUMNS: tuple[str, ...] = ("graph_id", "deployment_digest")
VALID_COLUMNS: tuple[str, ...] = (*LOCATOR_COLUMNS, "id")


def _sqlite_text_width(column: str, width: int) -> str:
    return (
        f"typeof({column}) = 'text' AND "
        f"length(CAST({column} AS BLOB)) IN ({width}, {2 * width})"
    )


SQLITE_KEYS: str = (
    _sqlite_text_width("graph_id", IDENTIFIER_LENGTH)
    + " AND "
    + _sqlite_text_width("deployment_digest", DIGEST_LENGTH)
)
SQLITE_ID_GRAMMAR: str = ascii_predicate(
    SQLITE, "id", ULID_ALPHABET, f"= {IDENTIFIER_LENGTH}", "0-7"
)
SQLITE_ID: str = (
    f"CASE WHEN {_sqlite_text_width('id', IDENTIFIER_LENGTH)} "
    f"THEN {SQLITE_ID_GRAMMAR} ELSE 0 END"
)
SQLITE_VALID: str = f"{SQLITE_KEYS} AND ({SQLITE_ID}) IS 1"
SQLITE_INVALID: str = f"{SQLITE_KEYS} AND ({SQLITE_ID}) IS NOT 1"
