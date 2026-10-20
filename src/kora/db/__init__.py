from kora.observability import CATALOG, EventSpec, FieldKind, Level

DB_COMPONENT: str = CATALOG.register_component("db")
DB_LOGGER_NAME: str = "kora.db"

MIGRATION_STARTED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.migration.started",
        component=DB_COMPONENT,
        level=Level.INFO,
        message="migration started",
        fields={
            "dialect": FieldKind.STRING,
            "from_revision": FieldKind.STRING,
            "to_revision": FieldKind.STRING,
        },
    )
)
MIGRATION_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.migration.finished",
        component=DB_COMPONENT,
        level=Level.INFO,
        message="migration finished",
        fields={
            "dialect": FieldKind.STRING,
            "revision": FieldKind.STRING,
            "steps": FieldKind.INTEGER,
            "duration_ms": FieldKind.FLOAT,
        },
    )
)
MIGRATION_FAILED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.migration.failed",
        component=DB_COMPONENT,
        level=Level.ERROR,
        message="migration failed",
        fields={"dialect": FieldKind.STRING, "exception": FieldKind.EXCEPTION},
    )
)
TRANSACTION_FAILED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.transaction.failed",
        component=DB_COMPONENT,
        level=Level.DEBUG,
        message="a transaction was rolled back",
        fields={"dialect": FieldKind.STRING, "exception": FieldKind.EXCEPTION},
    )
)

CLOSE_TIMED_OUT: EventSpec = CATALOG.register(
    EventSpec(
        name="db.close.timed_out",
        component=DB_COMPONENT,
        level=Level.WARNING,
        message="the database closed with units still in flight",
        fields={"dialect": FieldKind.STRING, "units_in_flight": FieldKind.INTEGER},
    )
)

__all__ = [
    "CLOSE_TIMED_OUT",
    "DB_COMPONENT",
    "DB_LOGGER_NAME",
    "MIGRATION_FAILED",
    "MIGRATION_FINISHED",
    "MIGRATION_STARTED",
    "TRANSACTION_FAILED",
]
