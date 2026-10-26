from typing import Final

from kora.observability import CATALOG, EventSpec, FieldKind, Level

COMPONENT: Final[str] = CATALOG.register_component("executor")
STEP_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "executor.step.finished",
        COMPONENT,
        Level.DEBUG,
        "a plan step finished",
        {"phase": FieldKind.STRING, "step": FieldKind.INTEGER},
    )
)
STEP_FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "executor.step.failed",
        COMPONENT,
        Level.WARNING,
        "a plan step failed",
        {
            "phase": FieldKind.STRING,
            "step": FieldKind.INTEGER,
            "failure_class": FieldKind.STRING,
            "code": FieldKind.STRING,
            "exit_code": FieldKind.INTEGER,
            "stderr_dropped": FieldKind.INTEGER,
        },
    )
)
CONTENT_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "executor.content.refused",
        COMPONENT,
        Level.WARNING,
        "published content was refused",
        {
            "phase": FieldKind.STRING,
            "failure_class": FieldKind.STRING,
            "code": FieldKind.STRING,
            "cause": FieldKind.STRING,
            "count": FieldKind.INTEGER,
        },
    )
)
PHASE_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "executor.phase.finished",
        COMPONENT,
        Level.INFO,
        "an executor phase finished",
        {"phase": FieldKind.STRING, "outcome": FieldKind.STRING},
    )
)
