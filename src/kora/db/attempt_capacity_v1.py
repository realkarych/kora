from kora.db.types import SQLITE

SQLITE_DEPLOYED_INDEX = "ix_graph_version_deployed_caps"
PHYSICAL_STATES = ("created", "claimed", "submitted", "running", "finishing")
PHYSICAL_ROWS = "status IN ('created', 'claimed', 'submitted', 'running', 'finishing')"
PG_PHYSICAL = (
    "((status)::text = ANY ((ARRAY["
    + ", ".join(f"'{state}'::character varying" for state in PHYSICAL_STATES)
    + "])::text[]))"
)
INDEXES: tuple[
    tuple[str, str, tuple[str, ...], str | None, str | None, str | None], ...
] = (
    (
        "ix_attempt_physical_capacity",
        "attempt",
        ("node_instance_id", "status", "runtime", "id"),
        PHYSICAL_ROWS,
        PHYSICAL_ROWS,
        PG_PHYSICAL,
    ),
    (
        "ix_attempt_run_recovery",
        "attempt",
        ("node_instance_id", "recovery_required", "id"),
        None,
        None,
        None,
    ),
    (
        "ix_graph_version_active_caps",
        "graph_version",
        ("graph_id", "number", "id"),
        "status = 'active'",
        "status = 'active'",
        "((status)::text = 'active'::text)",
    ),
    (
        SQLITE_DEPLOYED_INDEX,
        "graph_version",
        ("graph_id", "number", "id"),
        None,
        None,
        None,
    ),
)


def capacity_index_dialect(name: str) -> str | None:
    return SQLITE if name == SQLITE_DEPLOYED_INDEX else None
