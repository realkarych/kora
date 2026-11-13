ACTIVE_ATTEMPTS = "status IN ('claimed', 'submitted', 'running', 'finishing')"
DEADLINE_ROWS = (
    ACTIVE_ATTEMPTS + " AND deadline IS NOT NULL AND deadline_expired_at IS NULL"
)
LEASE_ROWS = ACTIVE_ATTEMPTS + " AND lease_until IS NOT NULL"
CANCEL_STATES = ("created", "claimed", "submitted", "running", "finishing")
CANCEL_ROWS = (
    "status IN ('created', 'claimed', 'submitted', 'running', 'finishing') "
    "AND desired_state <> 'cancel'"
)
PG_STATES = (
    "((status)::text = ANY ((ARRAY["
    + ", ".join(f"'{state}'::character varying" for state in CANCEL_STATES[1:])
    + "])::text[]))"
)
PG_CANCEL = (
    "((status)::text = ANY ((ARRAY["
    + ", ".join(f"'{state}'::character varying" for state in CANCEL_STATES)
    + "])::text[]))"
)
INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None, str | None], ...] = (
    (
        "ix_attempt_due_deadline",
        "attempt",
        ("deadline", "id"),
        DEADLINE_ROWS,
        f"({PG_STATES} AND (deadline IS NOT NULL) AND (deadline_expired_at IS NULL))",
    ),
    (
        "ix_attempt_due_lease",
        "attempt",
        ("lease_until", "id"),
        LEASE_ROWS,
        f"({PG_STATES} AND (lease_until IS NOT NULL))",
    ),
    (
        "ix_attempt_cancel_due",
        "attempt",
        ("node_instance_id", "id"),
        CANCEL_ROWS,
        f"({PG_CANCEL} AND ((desired_state)::text <> 'cancel'::text))",
    ),
    ("ix_wait_node_status", "wait", ("node_instance_id", "status", "id"), None, None),
    ("ix_approval_request_wait", "approval_request", ("wait_id",), None, None),
)
