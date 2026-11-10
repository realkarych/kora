from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate
from kora.db.types import IDENTIFIER_LENGTH, SQLITE

SQLITE_RUN_LOCATOR_INDEX: str = "ix_run_input_locator"
SQLITE_RUN_INPUT_LOCATOR_INDEX: str = "ix_run_inputs_locator"


def _canonical_key(column: str) -> str:
    characters = ascii_predicate(
        SQLITE, column, ULID_ALPHABET, f"= {IDENTIFIER_LENGTH}", "0-7"
    )
    return (
        f"CASE WHEN typeof({column}) = 'text' AND octet_length({column}) "
        f"IN ({IDENTIFIER_LENGTH}, {2 * IDENTIFIER_LENGTH}) "
        f"THEN ({characters}) ELSE 0 END"
    )


SQLITE_RUN_KEY: str = _canonical_key("id")
SQLITE_RUN_INPUT_KEY: str = _canonical_key("run_id")
