from typing import Literal

from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

DB_COMPONENT: str = CATALOG.register_component("db")
DB_LOGGER_NAME: str = "kora.db"

BOOTSTRAP_COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.bootstrap.completed",
        component=DB_COMPONENT,
        level=Level.INFO,
        message="bootstrap initialization completed",
        fields={"outcome": FieldKind.STRING},
    )
)

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

RUN_INPUT_PARENT_CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        name="db.run_input_parent.checked",
        component=DB_COMPONENT,
        level=Level.DEBUG,
        message="run input parent checked",
        fields={"verified": FieldKind.BOOLEAN},
    )
)

CUSTODY_TRANSITION: EventSpec = CATALOG.register(
    EventSpec(
        "db.custody.transition",
        DB_COMPONENT,
        Level.INFO,
        "native custody transition confirmed",
        {"state": FieldKind.STRING},
    )
)
CUSTODY_REFUSED: EventSpec = CATALOG.register(
    EventSpec(
        "db.custody.refused",
        DB_COMPONENT,
        Level.DEBUG,
        "native custody admission refused",
        {"state": FieldKind.STRING},
    )
)
CUSTODY_UNFINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "db.custody.unfinished",
        DB_COMPONENT,
        Level.WARNING,
        "native custody closure remains unfinished",
        {"state": FieldKind.STRING},
    )
)


CustodyDiagnostic = Literal[
    "hint_published",
    "successor_denial_persisted",
    "successor_registered",
    "native_turn",
    "ordinary_open",
    "ordinary_admission",
    "coordinator_admission",
    "coordinator_operation",
    "coordinator_closure",
]


class CustodyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("execution custody is unavailable")


def custody_log(spec: EventSpec, state: CustodyDiagnostic) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME).emit(spec, state=state)


__all__ = [
    "BOOTSTRAP_COMPLETED",
    "CLOSE_TIMED_OUT",
    "DB_COMPONENT",
    "DB_LOGGER_NAME",
    "MIGRATION_FAILED",
    "MIGRATION_FINISHED",
    "MIGRATION_STARTED",
    "RUN_INPUT_PARENT_CHECKED",
    "TRANSACTION_FAILED",
]
