from kora.observability import CATALOG, EventSpec, FieldKind, Level

CEL_COMPONENT: str = CATALOG.register_component("cel")
CEL_LOGGER_NAME: str = "kora.cel_eval"

BINDING_LOADED: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.binding.loaded",
        component=CEL_COMPONENT,
        level=Level.INFO,
        message="CEL binding loaded",
        fields={"version": FieldKind.STRING},
    )
)

EVALUATOR_STARTED: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.evaluator.started",
        component=CEL_COMPONENT,
        level=Level.INFO,
        message="CEL evaluation thread started",
        fields={},
    )
)
EVALUATOR_STOPPED: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.evaluator.stopped",
        component=CEL_COMPONENT,
        level=Level.INFO,
        message="CEL evaluator handle closed",
        fields={"rejected_jobs": FieldKind.INTEGER},
    )
)
EVALUATOR_FAILED: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.evaluator.failed",
        component=CEL_COMPONENT,
        level=Level.ERROR,
        message="CEL evaluation thread failed",
        fields={"fault": FieldKind.STRING},
    )
)
EVALUATOR_CLOSE_TIMED_OUT: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.evaluator.close_timed_out",
        component=CEL_COMPONENT,
        level=Level.WARNING,
        message="CEL evaluator close deadline exceeded",
        fields={},
    )
)
