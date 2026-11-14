from kora.db.types import SQLITE

SQLITE_OWNER_INDEX = "ix_wait_open_owner"
OPEN_ROWS = "status = 'open'"
DUE_ROWS = "status = 'open' AND kind = 'admission'"
INDEXES: tuple[tuple[str, str, tuple[str, ...], str, str, str], ...] = (
    (
        SQLITE_OWNER_INDEX,
        "wait",
        ("node_instance_id", "id", "kind", "attempt_id"),
        OPEN_ROWS,
        OPEN_ROWS,
        "((status)::text = 'open'::text)",
    ),
    (
        "ix_wait_admission_due",
        "wait",
        ("wake_at", "node_instance_id", "id"),
        DUE_ROWS,
        DUE_ROWS,
        "(((status)::text = 'open'::text) AND ((kind)::text = 'admission'::text))",
    ),
)


def wait_index_dialect(name: str) -> str | None:
    return SQLITE if name == SQLITE_OWNER_INDEX else None
